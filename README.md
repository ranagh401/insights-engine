# CRM Insights Engine

FastAPI service that turns a CRM Power BI dataset into narrative business insights: it computes KPIs with generated DAX, detects signals (anomalies, trends, threshold breaches) across dimensions, drills into *why* they happened, and writes LLM-generated narrative insights back to the database.

## How it works

1. **Config-driven**: KPIs, dimensions, signals, thresholds, and their relationships live in Postgres config tables (`models/`).
2. **DAX engine** (`dax/`): builds DAX queries for KPIs and dimension breakdowns and runs them against the Power BI dataset via the REST API (`powerbi/api_client.py`).
3. **Signal detection** (`engine/signal_detector.py`): evaluates per-dimension thresholds over the reporting period and records qualifying signals.
4. **Why analysis** (`engine/why_analyzer.py`): breaks a signal down across related dimensions to explain the movement.
5. **Narrative LLM** (`config/narrative_llm.py`): Azure OpenAI (or Azure-hosted Cohere) writes the final narrative insights.
6. **Scheduler / cron** (`scheduler/`, `scripts/weekly_refresh_cron.py`): weekly end-to-end refresh — trigger signals, wait, generate narratives, refresh rollups, and optionally email a summary (see `docs/weekly-refresh-cron.md`).

## API

Routes are under `/pbi` (Swagger at `/docs`):

- `kpis`, `signals`, `signal_jobs` — compute KPIs, run signal jobs
- `insights`, `portal` — narrative insights and portal-facing rollups
- `data` — dataset fetch helpers (week enrichment etc.)
- `trigger` — kick off end-to-end runs
- `health` — liveness

## Setup

```bash
pip install -e .
cp .env.example .env   # fill in your values
python main.py         # or: crm-insights-serve
```

Swagger: http://localhost:8014/docs (port from `PLATFORM_SERVICE__PORT`).

Key configuration (see `.env.example` for the full annotated list):

- `PLATFORM_DATABASE__URL` — Postgres for config + results
- `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `PBI_WORKSPACE_ID` / `PBI_DATASET_ID` — Power BI service principal + dataset
- `PLATFORM_OPENAI__*` — Azure OpenAI for narrative generation
- `WEEKLY_REFRESH__*` — cron behavior + optional SMTP summary email

## Layout

| Path | Purpose |
| --- | --- |
| `src/ms_crm_insights_portal/` | The service: API, config, DAX builder, engines, models, scheduler |
| `src/platform_core/` | Shared platform plumbing: settings, auth/JWT, DB, observability |
| `scripts/weekly_refresh_cron.py` | Weekly end-to-end refresh entry point |
| `docker/Dockerfile` | Container build |
| `tests/` | Query-builder unit tests + a live DAX KPI test harness |

## Development

```bash
make install   # editable install
make run       # start the service
make test      # pytest
```
