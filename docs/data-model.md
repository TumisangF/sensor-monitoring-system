# Data model

MongoDB database `sensor_db`. Every collection has schema validation (`strict`, action `error`),
so an invalid document is rejected at write time. The metric payload is deliberately open:
`metric_type` is free text and extra fields are allowed, so new sensor types (CO2, noise, fine dust)
need no schema change.

| Collection | Purpose | Required fields | Indexes |
|---|---|---|---|
| `sensor_readings` | One document per metric per accepted CSV row | `sensor_id`, `device_timestamp`, `metric_type`, `value` (also `batch_id`, `ingested_at`) | unique `uniq_reading` (sensor, time, metric); `metric_time` |
| `alerts` | Threshold alerts for operator review | `sensor_id`, `metric_type`, `value`, `threshold`, `condition`, `device_timestamp`, `created_at`, `resolved` | `active_recent`; `sensor_active` |
| `aggregated_metrics` | Hourly average, min, max and count per sensor and metric, recomputed from the readings | `sensor_id`, `metric_type`, `hour`, `avg_value`, `min_value`, `max_value`, `count`, `updated_at` | unique `uniq_hour`; `metric_hour` |
| `sensor_health` | Last-seen time and rejection counts per sensor | `sensor_id`, `last_seen`, `updated_at` (also `rejected_readings`, `rejected_by_metric`, `last_rejected_at`, `last_rejected_reason`, `last_batch_id`) | unique `uniq_sensor` |
| `ingestion_batches` | One record per ingested batch | `batch_id`, `file`, `started_at`, `outcome`, `input_rows`, `accepted_rows`, `rejected_rows` (also `run_id`, `batch_number`, `inserted_documents`, `duplicate_documents`, `error_reason`, `interrupted`, `last_completed_batch`) | unique `uniq_batch`; `recent` |

`alerts.condition` is `exceeds_maximum` or `below_minimum`.
`ingestion_batches.outcome` is `completed`, `completed_with_errors` or `failed`.

## Example reading

```json
{
  "sensor_id": "00:0f:00:70:91:0a",
  "device_timestamp": "2020-07-12T00:00:00Z",
  "metric_type": "temperature",
  "value": 22.4
}
```

A future sensor needs no migration:

```json
{
  "sensor_id": "node-12",
  "device_timestamp": "2026-11-09T09:30:00Z",
  "metric_type": "co2",
  "value": 415,
  "unit": "ppm",
  "location": "depot"
}
```

## Configuration

`config/thresholds.json` holds, per metric, the alert limits (`alert_min`, `alert_max`) and the
plausible range used to reject impossible readings (`valid_min`, `valid_max`). Edit the file and
restart the ingestion job; no code change is needed. The loader rejects inconsistent values
(for example a minimum above a maximum) with a clear message.

## Database users

- `admin` (root): used only by `db-init` to create the structure and by administrators.
- `sensor_app`: read/write on `sensor_db` only. The ingestion and API services connect as this user.

`db-init` is idempotent. It runs on every `docker compose up`, so it can be applied to an existing
database without losing data.

## Ingestion rules

A CSV row is accepted only if all of the following hold; otherwise the whole row is rejected, so stored
readings = accepted rows x 7.

| Reason code | Meaning |
|---|---|
| `missing_device` | No device ID |
| `bad_timestamp` | Missing, not numeric, or outside 2000 to 2100 |
| `missing_value:<metric>` | The value is empty |
| `not_a_number:<metric>` | The value is not numeric or is NaN or infinite |
| `invalid_value:<metric>` | A light or motion value that is not true, false, 1 or 0 |
| `out_of_range:<metric>` | Outside `valid_min` and `valid_max` in `config/thresholds.json` |

Light and motion are stored as 1.0 and 0.0.

A batch is `completed` when no row was rejected, `completed_with_errors` when some were, and `failed` when the
run stopped on an error. A failed batch is recorded with `interrupted: true` and the last completed batch.
Rejections are counted per sensor and metric in `sensor_health`, so a sensor that keeps sending bad values can be
shown as Degraded.

## Aggregates and reloads

Readings are unique on sensor, timestamp and metric, so loading a file twice stores nothing twice. After each
batch the hourly aggregates for the touched hours are recomputed from `sensor_readings` and replaced, so they are
always consistent with the stored readings, including after an interrupted run.
