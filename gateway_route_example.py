"""Paste/adapt this route inside the existing SageWire Gateway."""
import os
import requests
from flask import jsonify, request

RFID_SERVICE_URL = os.getenv("RFID_SERVICE_URL", "https://rfid.sagewire.dev").rstrip("/")

@app.post("/rfid/read")
def rfid_read():
    try:
        response = requests.post(
            f"{RFID_SERVICE_URL}/events/read",
            json=request.get_json(force=True),
            timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": "rfid_service_unavailable", "detail": str(exc)}), 502

    if "application/json" in response.headers.get("content-type", ""):
        return jsonify(response.json()), response.status_code
    return response.text, response.status_code
