import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alerts'))
from engine import AlertState, breach

T = datetime(2024, 1, 1, tzinfo=timezone.utc)
def reading(n, value):
    return {'device_timestamp': T + timedelta(minutes=n), 'value': value}

class EngineTests(unittest.TestCase):
    def test_boundaries(self):
        rule = {'alert_min': 10, 'alert_max': 20}
        self.assertIsNone(breach(10, rule))
        self.assertIsNone(breach(20, rule))
        self.assertEqual(breach(21, rule)[0], 'exceeds_maximum')
        self.assertEqual(breach(9, rule)[0], 'below_minimum')

    def test_open_hold_close(self):
        s = AlertState({'alert_max': 20})
        events = [e[0] for r in [reading(0, 19), reading(1, 21), reading(2, 22), reading(3, 20)] for e in s.feed(r)]
        self.assertEqual(events, ['open', 'close'])
        self.assertIsNone(s.open)

    def test_opposite_breach(self):
        s = AlertState({'alert_min': 10, 'alert_max': 20})
        events = [e[0] for r in [reading(0, 9), reading(1, 21)] for e in s.feed(r)]
        self.assertEqual(events, ['open', 'close', 'open'])

    def test_unresolved_at_end(self):
        s = AlertState({'alert_max': 20})
        list(s.feed(reading(0, 21)))
        self.assertIsNotNone(s.open)

if __name__ == '__main__':
    unittest.main()
