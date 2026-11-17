import csv
import os
import tempfile
from unittest.mock import patch

from pymongo import MongoClient
import pipeline
import thresholds

client = MongoClient(
    "mongodb://m8-recovery-mongo:27017",
    serverSelectionTimeoutMS=5000,
)
client.admin.command("ping")

db = client["m8_recovery_test"]
db.drop_collection("sensor_readings")
db.drop_collection("aggregated_metrics")
db.drop_collection("sensor_health")
db.drop_collection("ingestion_batches")
db.drop_collection("rejection_events")

pipeline.ensure_indexes(db)
rules = thresholds.load()

fields = ["device", "ts", "temp", "humidity",
          "co", "lpg", "smoke", "light", "motion"]

with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, "recovery_fixture.csv")

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i in range(3):
            writer.writerow({
                "device": "m8-test-sensor",
                "ts": 1595203200 + i * 60,
                "temp": 23,
                "humidity": 45,
                "co": 0.001,
                "lpg": 0.001,
                "smoke": 0.001,
                "light": "true",
                "motion": "false",
            })

    original = pipeline._update_aggregates
    calls = 0

    def interrupt_second_batch(db_arg, docs, now):
        global calls
        calls += 1
        if calls == 2:
            raise RuntimeError("SIMULATED aggregate update failure")
        return original(db_arg, docs, now)

    with patch.object(
        pipeline, "_update_aggregates",
        side_effect=interrupt_second_batch
    ):
        first = pipeline.process_file(db, path, rules, batch_size=2)

    print("FIRST RUN:", first["status"])
    assert first["status"] == "failed"

    second = pipeline.process_file(db, path, rules, batch_size=2)
    print("RESTART:", second["status"])
    assert second["status"] == "completed"

    readings = db.sensor_readings.count_documents({})
    aggregate_count = sum(
        x["count"] for x in db.aggregated_metrics.find(
            {}, {"count": 1}
        )
    )

    print("STORED READINGS:", readings)
    print("AGGREGATED READING COUNT:", aggregate_count)
    print("DUPLICATES ON RESTART:", second["duplicates"])

    assert readings == 21
    assert second["duplicates"] == 21

    if aggregate_count == readings:
        print("PASS: Recovery preserved aggregate consistency")
    else:
        print("RECOVERY GAP DETECTED:")
        print("Readings exist without corresponding aggregates.")
        print("Expected:", readings, "Actual:", aggregate_count)

client.close()
