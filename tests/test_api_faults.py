"""Isolated API tests: no database or network connection required."""
import asyncio
import json
import unittest
from unittest.mock import patch
from pymongo.errors import ServerSelectionTimeoutError
import main


class ApiFaultTests(unittest.TestCase):
    def test_health_success(self):
        class DB:
            def command(self, name):
                assert name == 'ping'
                return {'ok': 1}
        with patch.object(main, 'get_db', return_value=DB()):
            self.assertEqual(main.health(), {'status': 'ok'})

    def test_database_failure_is_sanitized(self):
        exc = ServerSelectionTimeoutError('secret-host:27017 password=secret')
        response = asyncio.run(main.mongo_error_handler(None, exc))
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.body)
        self.assertEqual(body['error'], 'database_unavailable')
        self.assertNotIn('secret', response.body.decode())

    def test_health_failure_propagates_to_shared_handler(self):
        class DB:
            def command(self, name):
                raise ServerSelectionTimeoutError('database offline')
        with patch.object(main, 'get_db', return_value=DB()):
            with self.assertRaises(ServerSelectionTimeoutError):
                main.health()


if __name__ == '__main__':
    unittest.main(verbosity=2)
