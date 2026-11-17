"""Pure and mocked ingestion tests; does not access MongoDB."""
import unittest
from unittest.mock import Mock
from pymongo.errors import BulkWriteError
import pipeline


class IngestionFaultTests(unittest.TestCase):
    def setUp(self):
        self.row = {'device': 'test-device', 'ts': '1594512000', 'temp': '22',
                    'humidity': '40', 'co': '0.01', 'lpg': '0.01',
                    'smoke': '0.01', 'light': 'false', 'motion': 'true'}
        self.rules = {}

    def test_valid_row_produces_seven_documents(self):
        result = pipeline.parse_row(self.row, self.rules)
        self.assertEqual(result['reasons'], [])
        self.assertEqual(len(result['docs']), 7)

    def test_missing_metric_rejects_entire_row(self):
        row = dict(self.row, smoke='')
        result = pipeline.parse_row(row, self.rules)
        self.assertIn('missing_value:smoke', result['reasons'])
        self.assertEqual(result['docs'], [])

    def test_nonfinite_value_rejected(self):
        row = dict(self.row, co='nan')
        result = pipeline.parse_row(row, self.rules)
        self.assertIn('not_a_number:co', result['reasons'])

    def test_duplicate_key_is_skipped(self):
        collection = Mock()
        collection.insert_many.side_effect = BulkWriteError({
            'writeErrors': [{'code': 11000, 'errmsg': 'duplicate key'}],
            'nInserted': 1,
        })
        self.assertEqual(pipeline._flush(collection, [{'a': 1}, {'a': 2}]), (1, 1))

    def test_nonduplicate_write_error_propagates(self):
        collection = Mock()
        collection.insert_many.side_effect = BulkWriteError({
            'writeErrors': [{'code': 121, 'errmsg': 'document validation failed'}],
            'nInserted': 0,
        })
        with self.assertRaises(BulkWriteError):
            pipeline._flush(collection, [{'a': 1}])


if __name__ == '__main__':
    unittest.main(verbosity=2)
