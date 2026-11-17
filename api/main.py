"""Sensor Monitoring API with controlled MongoDB failure responses (Milestone 7)."""
import os
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

app = FastAPI(title="Sensor Monitoring API", version="1.0.0")
_client = None


def get_db():
    global _client
    if _client is None:
        _client = MongoClient(
            host=os.getenv("MONGO_HOST", "mongodb"),
            username=os.environ["MONGO_USER"],
            password=os.environ["MONGO_PASSWORD"],
            authSource=os.getenv("MONGO_AUTH_SOURCE", "sensor_db"),
            serverSelectionTimeoutMS=5000,
        )
    return _client[os.getenv("MONGO_DB", "sensor_db")]


def get_readings():
    return get_db()["sensor_readings"]


@app.exception_handler(PyMongoError)
async def mongo_error_handler(request, exc):
    """Return a predictable error without exposing MongoDB credentials or internals."""
    return JSONResponse(status_code=503, content={
        "status": "error",
        "error": "database_unavailable",
        "message": "Monitoring data is temporarily unavailable. Try again later.",
    })


@app.get("/api/health")
def health():
    """Connectivity check; database errors are handled by the shared handler."""
    get_db().command("ping")
    return {"status": "ok"}


@app.get("/api/query")
def query(
    metric: str = Query("temperature", description="Metric type, e.g. temperature"),
    sensor_id: Optional[str] = Query(None, description="Limit to one sensor; default is all sensors"),
    limit: int = Query(1000, ge=1, le=5000, description="Maximum number of hourly points"),
):
    """Hourly average of one metric, oldest first, as rows of {time, value}."""
    match = {"metric_type": metric}
    if sensor_id:
        match["sensor_id"] = sensor_id
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%dT%H:00:00Z", "date": "$device_timestamp"}},
            "avg": {"$avg": "$value"},
        }},
        {"$sort": {"_id": 1}},
        {"$limit": limit},
    ]
    return [{"time": row["_id"], "value": round(row["avg"], 4)}
            for row in get_readings().aggregate(pipeline)]


from milestone5 import make_router
app.include_router(make_router(get_db))
