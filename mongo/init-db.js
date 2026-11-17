// Idempotent database setup: collections with schema validation, indexes, and the application user.
// Run by the db-init service on every start. Safe on a new or an existing database.
const dbName = process.env.MONGO_DB || "sensor_db";
const appUser = process.env.APP_DB_USER;
const appPassword = process.env.APP_DB_PASSWORD;
if (!appUser || !appPassword) {
  throw new Error("APP_DB_USER and APP_DB_PASSWORD must be set");
}
const sdb = db.getSiblingDB(dbName);

// ---- reusable field types ----
const text = { bsonType: "string", minLength: 1 };
const date = { bsonType: "date" };
const number = { bsonType: "number" };
const count = { bsonType: ["int", "long"], minimum: 0 };

// ---- schemas ----
// The metric payload is deliberately open: metric_type is free text and extra fields are allowed,
// so new sensor types (CO2, noise, fine dust) need no schema change.
const schemas = {
  sensor_readings: {
    bsonType: "object",
    required: ["sensor_id", "device_timestamp", "metric_type", "value"],
    properties: { sensor_id: text, device_timestamp: date, metric_type: text, value: number,
                  batch_id: text, ingested_at: date },
  },
  alerts: {
    bsonType: "object",
    required: ["sensor_id", "metric_type", "value", "threshold", "condition",
               "device_timestamp", "created_at", "resolved"],
    properties: { sensor_id: text, metric_type: text, value: number, threshold: number,
                  condition: { enum: ["exceeds_maximum", "below_minimum"] },
                  device_timestamp: date, created_at: date, resolved: { bsonType: "bool" },
                  resolved_at: date, batch_id: text },
  },
  aggregated_metrics: {
    bsonType: "object",
    required: ["sensor_id", "metric_type", "hour", "avg_value", "min_value", "max_value", "count", "updated_at"],
    properties: { sensor_id: text, metric_type: text, hour: date, avg_value: number,
                  min_value: number, max_value: number, count: count, updated_at: date },
  },
  sensor_health: {
    bsonType: "object",
    required: ["sensor_id", "last_seen", "updated_at"],
    properties: { sensor_id: text, last_seen: date, updated_at: date,
                  rejected_readings: count, last_batch_id: text,
                  rejected_by_metric: { bsonType: "object" }, last_rejected_at: date,
                  last_rejected_reason: { bsonType: "string" } },
  },
  rejection_events: {
    bsonType: "object",
    required: ["event_key", "sensor_id", "file", "line", "reasons", "created_at"],
    properties: { event_key: text, sensor_id: text, file: text, line: count,
                  reasons: { bsonType: "array", items: text }, created_at: date },
  },
  ingestion_batches: {
    bsonType: "object",
    required: ["batch_id", "file", "started_at", "outcome", "input_rows", "accepted_rows", "rejected_rows"],
    properties: { batch_id: text, file: text, started_at: date, finished_at: date,
                  outcome: { enum: ["completed", "completed_with_errors", "failed"] },
                  input_rows: count, accepted_rows: count, rejected_rows: count,
                  run_id: text, batch_number: count, inserted_documents: count, duplicate_documents: count,
                  error_reason: { bsonType: "string" }, interrupted: { bsonType: "bool" },
                  last_completed_batch: { bsonType: "string" } },
  },
};

function ensureCollection(name, schema) {
  const options = { validator: { $jsonSchema: schema }, validationLevel: "strict", validationAction: "error" };
  if (sdb.getCollectionNames().includes(name)) {
    const result = sdb.runCommand(Object.assign({ collMod: name }, options));
    if (result.ok !== 1) throw new Error("collMod failed for " + name + ": " + tojson(result));
    print("updated collection " + name);
  } else {
    sdb.createCollection(name, options);
    print("created collection " + name);
  }
}
for (const [name, schema] of Object.entries(schemas)) ensureCollection(name, schema);

// ---- indexes (names and options match what the ingestion service creates) ----
sdb.sensor_readings.createIndex({ sensor_id: 1, device_timestamp: 1, metric_type: 1 }, { unique: true, name: "uniq_reading" });
sdb.sensor_readings.createIndex({ metric_type: 1, device_timestamp: 1 }, { name: "metric_time" });
sdb.alerts.createIndex({ resolved: 1, created_at: -1 }, { name: "active_recent" });
sdb.alerts.createIndex({ sensor_id: 1, resolved: 1, created_at: -1 }, { name: "sensor_active" });
sdb.aggregated_metrics.createIndex({ sensor_id: 1, metric_type: 1, hour: 1 }, { unique: true, name: "uniq_hour" });
sdb.aggregated_metrics.createIndex({ metric_type: 1, hour: 1 }, { name: "metric_hour" });
sdb.sensor_health.createIndex({ sensor_id: 1 }, { unique: true, name: "uniq_sensor" });
sdb.rejection_events.createIndex({ event_key: 1 }, { unique: true, name: "uniq_rejection_event" });
sdb.ingestion_batches.createIndex({ batch_id: 1 }, { unique: true, name: "uniq_batch" });
sdb.ingestion_batches.createIndex({ started_at: -1 }, { name: "recent" });
print("indexes ensured");

// ---- application user: read/write on the sensor database only, no administrative rights ----
const roles = [{ role: "readWrite", db: dbName }];
if (sdb.getUser(appUser)) {
  sdb.updateUser(appUser, { pwd: appPassword, roles: roles });
  print("updated user " + appUser);
} else {
  sdb.createUser({ user: appUser, pwd: appPassword, roles: roles });
  print("created user " + appUser);
}
print("db-init complete");
