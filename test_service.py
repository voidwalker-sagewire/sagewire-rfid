import os
import tempfile

f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
f.close()
os.environ["RFID_DATABASE_PATH"] = f.name
os.environ["RFID_DEDUPE_SECONDS"] = "2.0"

from fastapi.testclient import TestClient
import server

client = TestClient(server.app)


def setup_function():
    with server.db() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM readers")


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["version"] == "0.2.0"


def test_register_read_and_duplicate():
    registered = client.post("/readers/register", json={"reader_id": "c316h-001", "name": "Test C316H", "technology": "UHF", "location_id": "test-chute"})
    assert registered.status_code == 200
    event = {"technology": "UHF", "reader_id": "c316h-001", "antenna_id": "antenna-1", "tag_id": "e20034120123456789000001", "observed_at": "2026-07-24T12:00:00Z"}
    first = client.post("/events/read", json=event)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    event["observed_at"] = "2026-07-24T12:00:01Z"
    second = client.post("/events/read", json=event)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
