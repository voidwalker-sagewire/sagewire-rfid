# SageWire RFID

**Version 0.1.0** — the first SageWire Device Service.

SageWire RFID accepts normalized read events from local hardware bridges, supports UHF and LF technologies, tracks reader health, filters rapid duplicates, and stores event history.

```text
RFID hardware → local bridge → SageWire Gateway → RFID Service → Sentinel / HerdMate
```

The local bridge handles Chainway SDK, Bluetooth, serial, or other hardware protocols. This service does not resolve tags to animals, operate gates, or depend on HerdMate or Sentinel.

## Endpoints

```text
GET  /health
GET  /version
POST /readers/register
POST /readers/{reader_id}/heartbeat
GET  /readers
GET  /readers/{reader_id}
POST /events/read
GET  /events
GET  /events/latest
```

## Supported technologies

```text
UHF
LF_FDXB
LF_HDX
LF_OTHER
```

Aliases `FDXB`, `FDX_B`, `HDX`, and `LF` are accepted.

## Local Docker test

```bash
docker build -t sagewire-rfid .
docker run --rm -p 5000:5000 \
  -v sagewire-rfid-data:/data \
  -e RFID_DEDUPE_SECONDS=2.0 \
  sagewire-rfid
```

```bash
curl -s http://localhost:5000/health
```

## Register the C316H

```bash
curl -s -X POST http://localhost:5000/readers/register \
  -H "Content-Type: application/json" \
  -d '{
    "reader_id": "c316h-001",
    "name": "DCC Portable C316H",
    "technology": "UHF",
    "model": "Chainway C316H",
    "serial_number": "REPLACE-ME",
    "location_id": "dcc-main-chute",
    "metadata": {
      "antenna_count": 4,
      "bridge": "sentinel-android"
    }
  }'
```

## Heartbeat

```bash
curl -s -X POST http://localhost:5000/readers/c316h-001/heartbeat \
  -H "Content-Type: application/json" -d '{}'
```

## Send a UHF read

```bash
curl -s -X POST http://localhost:5000/events/read \
  -H "Content-Type: application/json" \
  -d '{
    "technology": "UHF",
    "reader_id": "c316h-001",
    "antenna_id": "antenna-1",
    "tag_id": "E20034120123456789000001",
    "raw_tag": "E20034120123456789000001",
    "rssi": -47,
    "location_id": "dcc-main-chute"
  }'
```

## Duplicate rule

A read is marked duplicate when the reader, antenna, and normalized tag match within the configured window. Default: two seconds.

Duplicates are stored for diagnostics but excluded from normal listings.

```bash
curl -s "http://localhost:5000/events?limit=100"
curl -s "http://localhost:5000/events?limit=100&include_duplicates=true"
curl -s http://localhost:5000/events/latest
```

## Coolify

1. Create the GitHub repository `sagewire-rfid`.
2. Put these files in the repository root.
3. Create a Coolify application using the **Dockerfile** build pack.
4. Add persistent storage mounted at `/data`.
5. Add:

```text
RFID_DATABASE_PATH=/data/rfid.db
RFID_DEDUPE_SECONDS=2.0
```

6. Assign `https://rfid.sagewire.dev`.
7. Deploy and test:

```bash
curl -s https://rfid.sagewire.dev/health
```

## First proof test

```text
1. Health
2. Register reader
3. Heartbeat
4. Post fake UHF read
5. Post same read again within two seconds
6. Confirm duplicate=true
7. Fetch latest event
8. Fetch history including duplicates
```

After the direct service works, add the example thin Gateway route and expose:

```text
POST https://api.sagewire.dev/rfid/read
```
