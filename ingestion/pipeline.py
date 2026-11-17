"""Batch ingestion pipeline.

For every batch of CSV rows: validate each row, store one document per metric, recompute the hourly
aggregates for the hours touched, update the per-sensor health record, and write a batch outcome
record. A row is accepted only if every field is valid, so stored readings = accepted rows x 7.
"""
import csv
import math
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, ReplaceOne, UpdateOne
from pymongo.errors import BulkWriteError

# metric_type -> CSV column
METRIC_FIELDS = {
    "temperature": "temp", "humidity": "humidity", "co": "co", "lpg": "lpg",
    "smoke": "smoke", "light": "light", "motion": "motion",
}
BOOLEAN_METRICS = {"light", "motion"}  # stored as 1.0 / 0.0
TRUE_VALUES, FALSE_VALUES = {"true", "1"}, {"false", "0"}
MIN_TS, MAX_TS = 946684800.0, 4102444800.0  # 2000-01-01 .. 2100-01-01 (UTC)
MAX_REJECTS_LOGGED = 20  # rejected rows printed per batch
HOUR_FORMAT = "%Y-%m-%dT%H:00:00Z"


def utc_now():
    return datetime.now(timezone.utc)


def parse_row(row, rules):
    """Validate one CSV row. Returns device, timestamp, the documents (only if valid) and any reasons."""
    reasons, bad_metrics = [], []
    device = (row.get("device") or "").strip()
    if not device:
        reasons.append("missing_device")

    when = None
    try:
        ts = float(row.get("ts"))
        if not math.isfinite(ts) or not (MIN_TS <= ts <= MAX_TS):
            raise ValueError("timestamp out of range")
        when = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        reasons.append("bad_timestamp")

    values = {}
    for metric, column in METRIC_FIELDS.items():
        raw = row.get(column)
        problem = None
        if raw is None or not str(raw).strip():
            problem = "missing_value"
        elif metric in BOOLEAN_METRICS:
            text = str(raw).strip().lower()
            if text in TRUE_VALUES:
                values[metric] = 1.0
            elif text in FALSE_VALUES:
                values[metric] = 0.0
            else:
                problem = "invalid_value"
        else:
            try:
                value = float(raw)
            except ValueError:
                problem = "not_a_number"
            else:
                rule = rules.get(metric, {})
                if not math.isfinite(value):
                    problem = "not_a_number"
                elif ("valid_min" in rule and value < rule["valid_min"]) or \
                     ("valid_max" in rule and value > rule["valid_max"]):
                    problem = "out_of_range"
                else:
                    values[metric] = value
        if problem:
            reasons.append(f"{problem}:{metric}")
            bad_metrics.append(metric)

    docs = []
    if not reasons:
        docs = [{"sensor_id": device, "device_timestamp": when, "metric_type": metric, "value": value}
                for metric, value in values.items()]
    return {"device": device or None, "when": when, "docs": docs, "reasons": reasons, "bad_metrics": bad_metrics}


def ensure_indexes(db):
    """Safety net; the db-init service normally creates these first (same names and options)."""
    db.sensor_readings.create_index(
        [("sensor_id", ASCENDING), ("device_timestamp", ASCENDING), ("metric_type", ASCENDING)],
        unique=True, name="uniq_reading")
    db.sensor_readings.create_index([("metric_type", ASCENDING), ("device_timestamp", ASCENDING)], name="metric_time")


def _flush(collection, docs):
    """Insert readings. Returns (inserted, duplicates); a duplicate is skipped, never stored twice."""
    if not docs:
        return 0, 0
    try:
        result = collection.insert_many(docs, ordered=False)
        return len(result.inserted_ids), 0
    except BulkWriteError as err:
        errors = err.details.get("writeErrors", [])
        duplicates = sum(1 for e in errors if e.get("code") == 11000)
        if duplicates != len(errors):
            raise  # a real write error, not just a duplicate
        return len(docs) - duplicates, duplicates


def _update_aggregates(db, docs, now):
    """Recompute hourly avg/min/max/count from sensor_readings for the hours these documents touch.

    Recomputing from the stored readings (instead of adding to old totals) keeps the aggregates
    correct after a reload or an interrupted run.
    """
    if not docs:
        return
    hours = sorted({d["device_timestamp"].replace(minute=0, second=0, microsecond=0) for d in docs})
    one_hour = timedelta(hours=1)
    if len(hours) <= 48:
        window = {"$or": [{"device_timestamp": {"$gte": h, "$lt": h + one_hour}} for h in hours]}
    else:
        window = {"device_timestamp": {"$gte": hours[0], "$lt": hours[-1] + one_hour}}
    match = {"sensor_id": {"$in": sorted({d["sensor_id"] for d in docs})},
             "metric_type": {"$in": sorted({d["metric_type"] for d in docs})}, **window}
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": {"sensor_id": "$sensor_id", "metric_type": "$metric_type",
                    "hour": {"$dateToString": {"format": HOUR_FORMAT, "date": "$device_timestamp"}}},
            "avg": {"$avg": "$value"}, "min": {"$min": "$value"}, "max": {"$max": "$value"}, "count": {"$sum": 1},
        }},
    ]
    ops = []
    for g in db.sensor_readings.aggregate(pipeline, allowDiskUse=True):
        key = g["_id"]
        hour = datetime.strptime(key["hour"], HOUR_FORMAT).replace(tzinfo=timezone.utc)
        doc = {"sensor_id": key["sensor_id"], "metric_type": key["metric_type"], "hour": hour,
               "avg_value": g["avg"], "min_value": g["min"], "max_value": g["max"],
               "count": g["count"], "updated_at": now}
        ops.append(ReplaceOne({"sensor_id": doc["sensor_id"], "metric_type": doc["metric_type"], "hour": hour},
                              doc, upsert=True))
    if ops:
        db.aggregated_metrics.bulk_write(ops, ordered=False)


def _update_sensor_health(db, seen, rejected, batch_id, now):
    """Last-seen per sensor, plus rejection counts that later feed the Degraded status."""
    ops = []
    for sensor, last_ts in seen.items():
        update = {"$max": {"last_seen": last_ts}, "$set": {"updated_at": now, "last_batch_id": batch_id}}
        info = rejected.get(sensor)
        if info:
            update["$inc"] = {"rejected_readings": info["rows"]}
            for metric, count in info["by_metric"].items():
                update["$inc"][f"rejected_by_metric.{metric}"] = count
            update["$set"]["last_rejected_reason"] = info["last_reason"]
            if info["last_at"]:
                update["$max"]["last_rejected_at"] = info["last_at"]
        ops.append(UpdateOne({"sensor_id": sensor}, update, upsert=True))
    if ops:
        db.sensor_health.bulk_write(ops, ordered=False)


def _run_batch(db, lines, number, file_name, run_id, rules, now):
    batch_id = f"{run_id}-{number:04d}"
    started_at = now()
    docs, seen, rejected, reason_counts, rejected_rows = [], {}, {}, Counter(), []
    accepted = 0
    for line_no, row in lines:
        parsed = parse_row(row, rules)
        device, when = parsed["device"], parsed["when"]
        if device and when and (device not in seen or when > seen[device]):
            seen[device] = when
        if parsed["reasons"]:
            rejected_rows.append((line_no, parsed["reasons"]))
            reason_counts.update(parsed["reasons"])
            if device:
                # One persistent event per source row, even across separate ingestion runs.
                # Only newly created events increment sensor-health rejection counters.
                event_id = f"{file_name}:{line_no}:{device}"
                event = {
                    "_id": event_id, "event_key": event_id, "file": file_name, "line": line_no,
                    "sensor_id": device, "device_timestamp": when,
                    "reasons": parsed["reasons"], "bad_metrics": parsed["bad_metrics"],
                    "created_at": now(),
                }
                result = db.rejection_events.update_one(
                    {"_id": event_id}, {"$setOnInsert": event}, upsert=True
                )
                if result.upserted_id is not None:
                    info = rejected.setdefault(device, {"rows": 0, "by_metric": Counter(), "last_at": None, "last_reason": ""})
                    info["rows"] += 1
                    info["by_metric"].update(parsed["bad_metrics"])
                    info["last_reason"] = parsed["reasons"][0]
                    if when and (info["last_at"] is None or when > info["last_at"]):
                        info["last_at"] = when
        else:
            accepted += 1
            docs.extend(parsed["docs"])

    ingested_at = now()
    for doc in docs:
        doc["batch_id"] = batch_id
        doc["ingested_at"] = ingested_at
    inserted, duplicates = _flush(db.sensor_readings, docs)
    if docs:
        _update_aggregates(db, docs, ingested_at)
    rejected_count = len(rejected_rows)
    outcome = "completed_with_errors" if rejected_count else "completed"
    record = {"batch_id": batch_id, "run_id": run_id, "batch_number": number, "file": file_name,
              "started_at": started_at, "finished_at": now(), "outcome": outcome,
              "input_rows": len(lines), "accepted_rows": accepted, "rejected_rows": rejected_count,
              "inserted_documents": inserted, "duplicate_documents": duplicates, "interrupted": False}
    summary = ""
    if rejected_count:
        summary = ", ".join(f"{reason} x{n}" for reason, n in reason_counts.most_common(5))
        record["error_reason"] = f"{rejected_count} rows rejected: {summary}"
    db.ingestion_batches.insert_one(record)

    print(f"batch {number:04d} {outcome.replace('_', ' ')}: input {len(lines)}, accepted {accepted}, "
          f"rejected {rejected_count}, {inserted} documents inserted, {duplicates} duplicates", flush=True)
    for line_no, reasons in rejected_rows[:MAX_REJECTS_LOGGED]:
        print(f"  rejected CSV line {line_no}: {', '.join(reasons)}", flush=True)
    if len(rejected_rows) > MAX_REJECTS_LOGGED:
        print(f"  ... and {len(rejected_rows) - MAX_REJECTS_LOGGED} more rejected rows", flush=True)
    return batch_id, accepted, rejected_count, inserted, duplicates


def process_file(db, path, rules, batch_size=500, now=utc_now):
    """Ingest one CSV file. Returns totals including 'status': completed, completed_with_errors or failed."""
    file_name = os.path.basename(path)
    run_id = f"{os.path.splitext(file_name)[0]}-{now().strftime('%Y%m%dT%H%M%S%fZ')}"
    totals = {"rows": 0, "accepted": 0, "rejected": 0, "inserted": 0, "duplicates": 0,
              "batches": 0, "status": "completed", "run_id": run_id, "last_completed_batch": None,
              "error": None}
    started = time.time()
    buffer, number = [], 0

    def run(lines):
        nonlocal number
        number += 1
        try:
            batch_id, accepted, rejected, inserted, duplicates = _run_batch(db, lines, number, file_name, run_id, rules, now)
        except Exception as exc:  # noqa: BLE001 - any failure stops the run and is recorded
            totals["status"], totals["error"] = "failed", str(exc)
            try:
                db.ingestion_batches.insert_one({
                    "batch_id": f"{run_id}-{number:04d}", "run_id": run_id, "batch_number": number,
                    "file": file_name, "started_at": now(), "finished_at": now(), "outcome": "failed",
                    "input_rows": len(lines), "accepted_rows": 0, "rejected_rows": 0,
                    "error_reason": str(exc)[:500], "interrupted": True,
                    "last_completed_batch": totals["last_completed_batch"] or ""})
            except Exception:  # noqa: BLE001 - the database may be the thing that failed
                pass
            return False
        totals["rows"] += len(lines)
        totals["accepted"] += accepted
        totals["rejected"] += rejected
        totals["inserted"] += inserted
        totals["duplicates"] += duplicates
        totals["batches"] = number
        totals["last_completed_batch"] = batch_id
        return True

    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            buffer.append((reader.line_num, row))
            if len(buffer) == batch_size:
                if not run(buffer):
                    break
                buffer = []
        else:
            if buffer:
                run(buffer)
    if totals["status"] != "failed" and totals["rejected"]:
        totals["status"] = "completed_with_errors"
    totals["seconds"] = round(time.time() - started, 1)
    return totals
