# City Environmental Sensor Monitoring System

A containerised system that ingests environmental sensor data in batches, stores it in MongoDB,
raises threshold alerts for operator review, and shows sensor status and trends on one Grafana dashboard.

**Purpose:** early warning of fire and gas hazards at municipal sites, with historical trends for planning.
**Scope:** CSV batch ingestion and historical monitoring. It does not verify a fire or gas leak,
notify citizens, or dispatch emergency services.

## Status

| Milestone | State |
|---|---|
| 0. Foundation (MongoDB, health check, restart policy, volume) | In progress |
| 1. Walking skeleton | Planned |
| 2. Data model | Planned |
| 3. Ingestion pipeline | Planned |
| 4. Alert engine | Planned |
| 5. API | Planned |
| 6. Dashboard | Planned |
| 7. Reliability | Planned |
| 8. Tests and documentation | Planned |

## Prerequisites

- Docker Desktop (Windows or Mac) or Docker Engine with the Compose plugin (Linux)
- Git

## Quick start

```bash
git clone https://github.com/TumisangF/sensor-monitoring-system.git
cd sensor-monitoring-system
cp .env.example .env        # then edit .env and set a real password
docker compose up -d
```

Older installations use `docker-compose` instead of `docker compose`.

## Verify the foundation

```bash
bash scripts/verify_milestone0.sh
```

The script checks that MongoDB is healthy, that login works, that data survives a restart, and that
the restart policy is set. It should end with `All Milestone 0 checks passed`.

## Project layout

```
sensor-monitoring-system/
├── docker-compose.yml     # services, network, volume
├── .env.example           # configuration template (copy to .env)
├── api/                   # FastAPI service            (Milestone 1 onwards)
├── ingestion/             # batch ingestion service    (Milestone 1 onwards)
├── mongo/                 # database initialisation    (Milestone 2)
├── grafana/provisioning/  # dashboard provisioning     (Milestone 6)
├── data/                  # CSV files (not committed)
├── docs/                  # design documents
└── scripts/               # verification scripts
```

## Common commands

```bash
docker compose ps                 # service state and health
docker compose logs -f mongodb    # database logs
docker compose down               # stop (data kept)
docker compose down -v            # stop and wipe all data
```

## Dataset

Environmental Sensor Telemetry Data (Kaggle, garystafford/environmental-sensor-data-132k).
Place the CSV in `data/`. It is not committed to the repository.
