"""Milestone 4: build historical threshold-alert episodes from stored readings.

Run independently of ingestion. Uses existing MongoDB alert schema, no data migration.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError

from engine import AlertState


def run(db, rules, limit=0, dry_run=False, metric_filter=None):
    stats = {'readings': 0, 'opened': 0, 'closed': 0, 'streams': 0}
    sensors = db.sensor_readings.distinct('sensor_id')
    for sensor in sorted(sensors):
        for metric, rule in sorted(rules.items()):
            # A separate state per sensor and metric; chronological processing is essential.
            if metric_filter and metric != metric_filter:
                continue
            state = AlertState(rule)
            query = {'sensor_id': sensor, 'metric_type': metric}
            cursor = db.sensor_readings.find(query, {'_id': 0, 'sensor_id': 1,
                'metric_type': 1, 'device_timestamp': 1, 'value': 1, 'batch_id': 1}).sort('device_timestamp', ASCENDING).batch_size(2000)
            stats['streams'] += 1
            try:
                for reading in cursor:
                    stats['readings'] += 1
                    for action, episode, current in state.feed(reading):
                        started = episode.start
                        # Stable event ID makes replay idempotent. No changes to Mongo schema.
                        key = f"threshold:{sensor}:{metric}:{started['device_timestamp'].isoformat()}:{episode.condition}"
                        if action == 'open':
                            stats['opened'] += 1
                            if not dry_run:
                                db.alerts.update_one({'_id': key}, {'$setOnInsert': {
                                    'sensor_id': sensor, 'metric_type': metric,
                                    'value': started['value'], 'threshold': episode.threshold,
                                    'condition': episode.condition,
                                    'device_timestamp': started['device_timestamp'],
                                    'created_at': datetime.now(timezone.utc), 'resolved': False,
                                    'batch_id': started.get('batch_id', 'historical'),
                                }}, upsert=True)
                        else:
                            stats['closed'] += 1
                            if not dry_run:
                                # A normal reading (or opposite breach) ends the previous episode.
                                db.alerts.update_one({'_id': key}, {'$set': {
                                    'resolved': True, 'resolved_at': current['device_timestamp']}})
                    if limit and stats['readings'] >= limit:
                        return stats
            finally:
                cursor.close()
            print(f"Processed {sensor} / {metric}; total readings={stats['readings']}", flush=True)
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Compute only; do not write alerts')
    parser.add_argument('--limit', type=int, default=0, help='Maximum readings total (for smoke tests)')
    parser.add_argument('--metric', choices=['temperature', 'humidity', 'co', 'lpg', 'smoke'])
    parser.add_argument('--threshold-override', type=float)
    parser.add_argument(
        '--test-collection',
        action='store_true',
        help='Write alerts to a separate test collection'
)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
        parser.add_argument('--metric', choices=['temperature', 'humidity', 'co', 'lpg', 'smoke'])
        parser.add_argument('--threshold-override', type=float)
    # Reuse existing thresholds module from ingestion container.
    
    import thresholds
    rules = thresholds.load()
    if args.threshold_override is not None:
        if not args.metric:
            parser.error('--threshold-override requires --metric')
        if args.metric not in rules:
            parser.error('Metric has no configured alert rule')
        if not 0 <= args.threshold_override <= 1 and args.metric == 'smoke':
            parser.error('Smoke threshold must be between 0 and 1')
        rules[args.metric] = dict(rules[args.metric])
        rules[args.metric]['alert_max'] = args.threshold_override
    client = MongoClient(host=os.getenv('MONGO_HOST', 'mongodb'),
        username=os.environ['MONGO_USER'], password=os.environ['MONGO_PASSWORD'],
        authSource=os.getenv('MONGO_AUTH_SOURCE', 'sensor_db'),
        serverSelectionTimeoutMS=10000)
    db = client[os.getenv('MONGO_DB', 'sensor_db')]
    if args.test_collection:
        from types import SimpleNamespace
        db = SimpleNamespace(
            sensor_readings=db.sensor_readings,
            alerts=db.alerts_milestone4_test
        )
    try:
        client.admin.command('ping')
        stats = run(db, rules, args.limit, args.dry_run, args.metric)
        print(f"ALERTS DONE readings={stats['readings']} opened={stats['opened']} "
              f"closed={stats['closed']} streams={stats['streams']} dry_run={args.dry_run}", flush=True)
    except PyMongoError as exc:
        print(f"MongoDB error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
