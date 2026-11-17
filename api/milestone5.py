"""Milestone 5: read-only dashboard endpoints, independent of ingestion and alert engine.

Mount with: app.include_router(make_router(get_db))
"""
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pymongo import DESCENDING
from pymongo.errors import PyMongoError

from pydantic import BaseModel, Field
from pymongo import ReturnDocument

METRICS = ('temperature', 'humidity', 'smoke', 'co', 'lpg', 'light', 'motion')


def _iso(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return value


def _serialize(document):
    return {k: _iso(v) for k, v in document.items() if k != '_id'} | {'id': str(document['_id'])}

class AcknowledgeRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=100)

class FalsePositiveRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

class SiteCheckRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

def make_router(get_db: Callable):
    router = APIRouter(prefix='/api', tags=['Dashboard'])

    @router.get('/sensors')
    def sensors():
        """Registered sensors and latest observation times (historical dataset)."""
        db = get_db()
        items = list(db.sensor_health.find({}, {'_id': 0, 'sensor_id': 1, 'last_seen': 1,
            'rejected_readings': 1, 'last_rejected_reason': 1}).sort('sensor_id', 1))
        return {'count': len(items), 'sensors': [{**s, 'last_seen': _iso(s.get('last_seen'))} for s in items]}

    @router.get('/sensor-status')
    def sensor_status(stale_minutes: int = Query(60, ge=1, le=10080)):
        """Sensor availability relative to the historical dataset."""
        db = get_db()
        health = list(db.sensor_health.find({}, {'_id': 0}))

        reference = max(
            (h.get('last_seen') for h in health if h.get('last_seen')),
            default=None
        )
        cutoff = reference - timedelta(minutes=stale_minutes) if reference else None

        # Retrieve the latest stored value for each sensor and metric.
        latest_values = {}
        for h in health:
            sensor_id = h['sensor_id']
            latest_values[sensor_id] = {}

            for metric in ('temperature', 'humidity', 'smoke'):
                reading = db.sensor_readings.find_one(
                    {'sensor_id': sensor_id, 'metric_type': metric},
                    {'_id': 0, 'value': 1, 'device_timestamp': 1},
                    sort=[('device_timestamp', DESCENDING)]
                )
                latest_values[sensor_id][metric] = (
                    reading.get('value') if reading else None
                )

        items = []

        for h in sorted(health, key=lambda x: x['sensor_id']):
            sensor_id = h['sensor_id']
            last = h.get('last_seen')
            rejected = h.get('rejected_readings', 0)

            status = (
                'offline'
                if last is None or (cutoff and last < cutoff)
                else ('degraded' if rejected else 'online')
            )

            behind_minutes = (
                round(max(0, (reference - last).total_seconds() / 60), 2)
                if reference and last else None
            )

            alert_count = db.alerts.count_documents({
                'sensor_id': sensor_id
            })

            if status == 'offline':
                details = 'No recent data'
            elif status == 'degraded':
                details = h.get('last_rejected_reason') or 'Rejected readings recorded'
            else:
                details = 'All good'

            items.append({
                'sensor_id': sensor_id,
                'status': status,
                'last_seen': _iso(last),
                'behind_minutes': behind_minutes,
                'temperature': latest_values[sensor_id]['temperature'],
                'humidity': latest_values[sensor_id]['humidity'],
                'smoke': latest_values[sensor_id]['smoke'],
                'alert_count': alert_count,
                'details': details,
                'rejected_readings': rejected,
                'last_rejected_reason': h.get('last_rejected_reason')
            })

        counts = {
            s: sum(x['status'] == s for x in items)
            for s in ('online', 'degraded', 'offline')
        }

        return {
            'reference_time': _iso(reference),
            'stale_minutes': stale_minutes,
            'historical': True,
            'coverage_warning': (
                'Status is relative to the newest stored reading, '
                'not live connectivity'
            ),
            'sensors': items,
            'counts': counts
        }

    @router.get('/alerts')
    def alerts(resolved: Optional[bool] = None, sensor_id: Optional[str] = None,
               metric: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
        db = get_db()
        match = {}
        if resolved is not None: match['resolved'] = resolved
        if sensor_id: match['sensor_id'] = sensor_id
        if metric: match['metric_type'] = metric
        docs = db.alerts.find(match).sort('device_timestamp', DESCENDING).limit(limit)
        return {'alerts': [_serialize(d) for d in docs]}


    @router.get('/alerts/table')
    def alerts_table():
        """Display-ready unresolved alerts for the Grafana table."""
        db = get_db()
        docs = db.alerts.find(
            {'resolved': False}
        ).sort('device_timestamp', DESCENDING).limit(100)

        rows = [_serialize(d) for d in docs]

        if rows:
            return rows

        return [{
            'sensor_id': '✓ No unresolved threshold alerts',
            'metric_type': '',
            'value': None,
            'threshold': None,
            'condition': '',
            'device_timestamp': None
        }]

    @router.get('/alerts/summary')
    def alert_summary():
        db = get_db()
        return {'total': db.alerts.count_documents({}),
                'active': db.alerts.count_documents({'resolved': False}),
                'resolved': db.alerts.count_documents({'resolved': True}),
                'historical': True}

    @router.get('/trends')
    def trends(metric: str = Query('temperature'), sensor_id: Optional[str] = None,
               start: Optional[datetime] = None, end: Optional[datetime] = None,
               limit: int = Query(500, ge=1, le=5000)):
        """Hourly aggregates. Without sensor_id, combines sensors using weighted averages."""
        if metric not in METRICS:
            raise HTTPException(422, detail=f'metric must be one of: {", ".join(METRICS)}')
        if start and end and start > end:
            raise HTTPException(422, detail='start must be <= end')
        match = {'metric_type': metric}
        if sensor_id: match['sensor_id'] = sensor_id
        hour = {}
        if start: hour['$gte'] = start
        if end: hour['$lte'] = end
        if hour: match['hour'] = hour
        pipeline = [{'$match': match}, {'$group': {'_id': '$hour',
                    'weighted_sum': {'$sum': {'$multiply': ['$avg_value', '$count']}},
                    'count': {'$sum': '$count'}, 'min': {'$min': '$min_value'}, 'max': {'$max': '$max_value'}}},
                    {'$sort': {'_id': 1}}, {'$limit': limit}]
        points = [{'time': _iso(r['_id']), 'value': round(r['weighted_sum'] / r['count'], 6),
                   'min': r['min'], 'max': r['max'], 'count': r['count']}
                  for r in get_db().aggregated_metrics.aggregate(pipeline) if r['count']]
        return {'metric': metric, 'sensor_id': sensor_id, 'resolution': 'hour', 'points': points}


    @router.get('/overview')
    def overview():
        db = get_db()
        health = list(db.sensor_health.find(
            {}, {'_id': 0, 'sensor_id': 1, 'last_seen': 1}
        ))
        latest = max(
            (h.get('last_seen') for h in health if h.get('last_seen')),
            default=None
        )

        batch = db.ingestion_batches.find_one(
            {},
            {'_id': 0, 'outcome': 1, 'input_rows': 1,
             'accepted_rows': 1, 'rejected_rows': 1,
             'finished_at': 1, 'error_reason': 1},
            sort=[('finished_at', DESCENDING)]
        )

        if batch:
            batch['finished_at'] = _iso(batch.get('finished_at'))

        return {
            'sensor_count': len(health),
            'metric_readings_stored':
                db.sensor_readings.estimated_document_count(),
            'latest_batch': batch,
            'latest_dataset_timestamp': _iso(latest),
            'active_alerts': db.alerts.count_documents({'resolved': False}),
            'total_alerts': db.alerts.count_documents({}),
            'historical': True,
            'alert_note':
                'Threshold breaches require investigation; '
                'they do not confirm a fire or gas leak'
        }

    @router.post('/alerts/{alert_id}/acknowledge')
    def acknowledge_alert(alert_id: str, request: AcknowledgeRequest):
        """Record an operator's acknowledgement of an alert."""
        db = get_db()
        now = datetime.now(timezone.utc)

        alert = db.alerts.find_one_and_update(
            {
                '_id': alert_id,
                'acknowledged_at': {'$exists': False}
            },
            {
                '$set': {
                    'acknowledged': True,
                    'acknowledged_by': request.operator,
                    'acknowledged_at': now
                }
            },
            return_document=ReturnDocument.AFTER
        )

        if alert is None:
            existing = db.alerts.find_one({'_id': alert_id})

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail='Alert not found'
                )

            raise HTTPException(
                status_code=409,
                detail='Alert has already been acknowledged'
            )

        return {
            'success': True,
            'action': 'acknowledge',
            'alert_id': alert_id,
            'sensor_id': alert['sensor_id'],
            'acknowledged_by': request.operator,
            'acknowledged_at': _iso(now),
            'resolved': alert['resolved'],
            'message': 'Alert acknowledged successfully'
        }
    
    @router.post('/alerts/{alert_id}/false-positive')
    def mark_false_positive(alert_id: str, request: FalsePositiveRequest):
        """Record an investigated alert as a false positive."""
        db = get_db()
        now = datetime.now(timezone.utc)

        alert = db.alerts.find_one_and_update(
            {
                '_id': alert_id,
                'false_positive_at': {'$exists': False}
            },
            {
                '$set': {
                    'false_positive': True,
                    'false_positive_by': request.operator,
                    'false_positive_reason': request.reason,
                    'false_positive_at': now
                }
            },
            return_document=ReturnDocument.AFTER
        )

        if alert is None:
            existing = db.alerts.find_one({'_id': alert_id})

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail='Alert not found'
                )

            raise HTTPException(
                status_code=409,
                detail='Alert already marked as false positive'
            )

        return {
            'success': True,
            'action': 'mark_false_positive',
            'alert_id': alert_id,
            'sensor_id': alert['sensor_id'],
            'false_positive': True,
            'classified_by': request.operator,
            'reason': request.reason,
            'classified_at': _iso(now),
            'resolved': alert['resolved'],
            'message': 'Alert classified as false positive'
        }
    @router.post('/alerts/{alert_id}/site-check')
    def request_site_check(alert_id: str, request: SiteCheckRequest):
        """Create a maintenance site-check request linked to an alert."""
        db = get_db()

        alert = db.alerts.find_one({'_id': alert_id})

        if alert is None:
            raise HTTPException(
                status_code=404,
                detail='Alert not found'
            )

        now = datetime.now(timezone.utc)

        site_check = {
            'alert_id': alert_id,
            'sensor_id': alert['sensor_id'],
            'metric_type': alert['metric_type'],
            'requested_by': request.operator,
            'reason': request.reason,
            'requested_at': now,
            'status': 'pending'
        }

        result = db.site_checks.insert_one(site_check)

        return {
            'success': True,
            'action': 'request_site_check',
            'request_id': str(result.inserted_id),
            'alert_id': alert_id,
            'sensor_id': alert['sensor_id'],
            'requested_by': request.operator,
            'reason': request.reason,
            'requested_at': _iso(now),
            'status': 'pending',
            'message': 'Site check requested successfully'
        }

    return router
