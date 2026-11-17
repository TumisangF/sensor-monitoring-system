"""Batch ingestion service.

Reads every CSV in DATA_DIR in batches. Each batch is validated, stored (7 documents per accepted row),
aggregated per hour, recorded in sensor_health and summarised in ingestion_batches. Reloading a file
is safe: duplicate readings are skipped.
"""
import glob
import os
import sys

from pymongo import MongoClient

import pipeline
import thresholds

DATA_DIR = os.getenv("DATA_DIR", "/data")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500"))


def main():
    client = MongoClient(
        host=os.getenv("MONGO_HOST", "mongodb"),
        username=os.environ["MONGO_USER"],
        password=os.environ["MONGO_PASSWORD"],
        authSource=os.getenv("MONGO_AUTH_SOURCE", "sensor_db"),
        serverSelectionTimeoutMS=10000,
    )
    db = client[os.getenv("MONGO_DB", "sensor_db")]

    try:
        rules = thresholds.load()
    except (OSError, ValueError) as exc:
        print(f"Cannot load thresholds from {thresholds.config_path()}: {exc}", flush=True)
        return 1
    print(f"Thresholds loaded from {thresholds.config_path()}: {', '.join(sorted(rules))}", flush=True)
    pipeline.ensure_indexes(db)

    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    if not files:
        print(f"No CSV files found in {DATA_DIR}. Place a CSV there and run ingestion again.")
        return 0
    for path in files:
        name = os.path.basename(path)
        print(f"Processing {name} (batch size {BATCH_SIZE})", flush=True)
        t = pipeline.process_file(db, path, rules, BATCH_SIZE)
        print(f"FILE DONE {name}: rows={t['rows']} accepted={t['accepted']} rejected={t['rejected']} "
              f"inserted={t['inserted']} duplicates={t['duplicates']} seconds={t['seconds']} "
              f"outcome={t['status']}", flush=True)
        if t["status"] == "failed":
            print(f"INTERRUPTED in run {t['run_id']}: {t['error']}. "
                  f"Last completed batch: {t['last_completed_batch'] or 'none'}. "
                  f"Fix the cause and run ingestion again; completed batches are not duplicated.", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
