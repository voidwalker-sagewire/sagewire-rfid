import os
import tempfile
import unittest

f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
f.close()
os.environ["RFID_DATABASE_PATH"] = f.name
os.environ["RFID_DEDUPE_SECONDS"] = "2.0"

import server


class Tests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(f.name)
        except FileNotFoundError:
            pass

    def setUp(self):
        self.client = server.app.test_client()
        with server.db() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM readers")

    def register(self):
        return self.client.post("/readers/register", json={
            "reader_id": "c316h-001", "name": "Test C316H", "technology": "UHF", "location_id": "test-chute"
        })

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["service"], "sagewire-rfid")

    def test_register_read_and_duplicate(self):
        self.assertEqual(self.register().status_code, 201)
        event = {"technology": "UHF", "reader_id": "c316h-001", "antenna_id": "antenna-1",
                 "tag_id": "e20034120123456789000001", "observed_at": "2026-07-24T12:00:00Z"}
        first = self.client.post("/events/read", json=event)
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.get_json()["duplicate"])
        event["observed_at"] = "2026-07-24T12:00:01Z"
        second = self.client.post("/events/read", json=event)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])

    def test_unknown_reader(self):
        r = self.client.post("/events/read", json={"technology": "UHF", "reader_id": "missing", "tag_id": "ABC123"})
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
