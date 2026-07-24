# Weekly refresh cron

Unattended pipeline that takes the newest reporting week from Power BI all the way to
refreshed executive summaries, then emails stakeholders. Entry point:
[`scripts/weekly_refresh_cron.py`](../scripts/weekly_refresh_cron.py).

## What it does

| # | Step | How |
|---|------|-----|
| 1 | Resolve the period, apply it everywhere | `[Period]` DAX → `insights.portal_current_period` (frontend reads it) + every `config_signaljobsrenuitycrm.filters.period` |
| 2 | Generate signals + whys | `POST /pbi/trigger/run-all-signals`, then poll `signal_log`/`why_results` until they settle |
| 3 | Build main insights | `POST /pbi/insights/main-insights/generate-standard` → fills `maininsightscrm` (LLM) |
| 4 | Tag insight groups | `POST /pbi/portal/main-insights/backfill-group-names` |
| 5 | Refresh `finalcrm` | `POST /pbi/portal/main-insights/executive-summary/refresh` (LLM) |
| 6 | Refresh `finalcrmsalesrep` | `POST /pbi/portal/main-insights/executive-summary/sales-rep/refresh` (LLM) |
| 7 | Email the outcome | SMTP; skipped + logged if unconfigured |

Steps 3 and 4 are **not** in the original 5-step ask but are required: `finalcrm` reads
`maininsightscrm`, which only exists once main insights are generated from the fresh
signals, and the group-scoped summaries need `group_name` set. The pipeline aborts on the
first failing step and the email reports where it stopped.

## Period source (verified)

The model's `[Period]` measure returns a string like `"14-07-2026 to 21-07-2026"`
(`DD-MM-YYYY to DD-MM-YYYY`). The default DAX is `EVALUATE ROW("Period", [Period])`; the
parser also accepts explicit start/end columns, a single anchor date (→ 7-day window), and
a `"DD-DD Mon"` label. Override the query with `WEEKLY_REFRESH__PERIOD_DAX` if the measure
changes.

## Frontend — no redeploy per week

The KPI-card window is no longer hardcoded. The cron writes the period to
`insights.portal_current_period`; the frontend fetches it at runtime:

- Backend: `GET /pbi/portal/current-period` (added).
- Frontend: `fetchCurrentPeriod()` in
  `mf-insight-portal/src/pages/crm-control-tower/services/fetchCurrentPeriod.ts`, and
  `buildKpiCardsCompareRequest(baseFilters, kpis, period?)` now takes an optional period.

**One-time frontend wiring still needed:** the KPI-card fetch caller
(`crmControlTower.service.ts` ~line 578) should `await fetchCurrentPeriod()` and pass the
result as the third arg. Until then it uses the `KPI_CARDS_PERIOD` fallback constant. This
FE change ships on the next normal frontend deploy — the cron never touches frontend code.

## Configuration

All under `WEEKLY_REFRESH__*` in `.env` (see [.env.example](../.env.example)). The
important ones:

```dotenv
WEEKLY_REFRESH__API_BASE=http://localhost:8014     # the local service the cron calls
WEEKLY_REFRESH__SIGNAL_TIMEOUT_SECONDS=3600        # max wait for signals to settle
WEEKLY_REFRESH__HTTP_TIMEOUT_SECONDS=1800          # per LLM step
# Email (step 7) — email is skipped if host or recipients are blank:
WEEKLY_REFRESH__SMTP_HOST=
WEEKLY_REFRESH__SMTP_PORT=587
WEEKLY_REFRESH__SMTP_USER=
WEEKLY_REFRESH__SMTP_PASSWORD=
WEEKLY_REFRESH__EMAIL_FROM=
WEEKLY_REFRESH__EMAIL_TO=a@x.com,b@x.com
# Testing without Power BI — skips the DAX call:
# WEEKLY_REFRESH__PERIOD_OVERRIDE=2026-07-15:2026-07-21
```

## Running it

```bash
# Preview — walks the steps, no POSTs, no DB writes, no email:
python scripts/weekly_refresh_cron.py --dry-run

# Live:
python scripts/weekly_refresh_cron.py --run
```

Exit code is `0` on success, `1` if any step failed. Both paths email the report (when
SMTP is set).

## Schedule it (runs by itself)

The service is managed by PM2, but this job is a **separate process** that should run on a
timer, independent of the API's lifecycle. Two options:

**A. System crontab** (simplest). On the VM as `webadmin`:

```bash
crontab -e
# Mondays 06:00, from the repo, with output to a log:
0 6 * * 1 cd /home/webadmin/ms-insights-portal-crm && .venv/bin/python scripts/weekly_refresh_cron.py --run >> /home/webadmin/weekly_refresh.log 2>&1
```

**B. PM2 cron_restart** (keeps it beside the app). Add a second PM2 app that runs the
script and exits, with `cron_restart` set — good if you prefer everything under `pm2 list`.

Either way the job talks to the running API on `:8014` for the trigger/LLM steps, so the
service must be up when it fires.

## Cost & duration

Steps 3, 5, 6 are LLM-heavy. A full run generates main insights per KPI×dimension, then
~11 GPT-4o calls for `finalcrm` and up to ~500 for `finalcrmsalesrep` (2 per rep-group).
Expect minutes, not seconds. Scope the rep refresh with
`WEEKLY_REFRESH__SALES_REP_MIN_SIGNALS` if you want to skip the thin tail. The narrative
LLM is Azure GPT-4o today; swap to Claude when available by changing the
`main_insights_llm` default on the generate/refresh endpoints.

## New DB objects

- `insights.portal_current_period` — single row (`id=1`) holding the live window.
  Auto-created by `ensure_tables` on app start; already created on dev.
