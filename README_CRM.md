# MS Insights CRM (this fork)

This folder is a **full copy** of `ms-insights-portal-renuity` for **CRM-only** work.  
Change CRM-specific code here without affecting the main Renuity service.

## Setup

1. Copy environment from Renuity (or create from template):

   ```powershell
   cd ms-insights-portal-crm
   copy ..\\ms-insights-portal-renuity\\.env .env
   # or: copy .env.example .env
   ```

2. Use a **different port** than Renuity (default **8014** in `.env.example`):

   ```powershell
   $env:ONEPLATFORM_SERVICE__PORT = "8014"
   ```

3. Install and run:

   ```powershell
   pip install -e .
   python main.py
   # or: crm-insights-serve
   ```

4. Swagger: http://localhost:8014/docs

## Run alongside Renuity

| Service | Folder | Suggested port |
|---------|--------|----------------|
| Renuity | `ms-insights-portal-renuity` | 8010 |
| GenAI fork | `ms-insights-portal-renuity-genai` | 8013 |
| **CRM (this)** | `ms-insights-portal-crm` | **8014** |

Point the UI proxy at this port when working on CRM-only features:

```powershell
cd insights-portal-ui
$env:VITE_DEV_PBI_PROXY = "http://127.0.0.1:8014"
npm run dev
```

## CRM-specific behaviour (this fork)

- **No Renuity `Product_Lead = Bath` filter** on DAX queries.
- **KPIs load from `configkpisrenuitycrm` only** — `configderivedkpisrenuitycrm` is not used for signals/whys.
- Job slice filters must match **valid drill dimensions** on the KPI (`configkpivaliddimensionsrenuitycrm`).

## PostgreSQL config tables (CRM)

All metadata is read from the `insights` schema using the `*renuitycrm` suffix:

| Table | Purpose |
|-------|---------|
| `configkpisrenuitycrm` | KPI definitions |
| `configderivedkpisrenuitycrm` | Derived KPIs (API only; signals use base KPIs) |
| `config_dimensionsreunitycrm` | Dimension → PBI column mapping |
| `configkpivaliddimensionsrenuitycrm` | Valid dimensions per KPI |
| `configkpidependenciesrenuitycrm` | KPI dependency tree (WHY Route B) |
| `config_featuresrenuitycrm` | Feature definitions |
| `config_signalsrenuitycrm` | Signal thresholds / operators |
| `config_signaljobsrenuitycrm` | Signal jobs |
- **Insight narratives** are written to **`insights.maininsightscrm`** (override with `MAIN_INSIGHTS_TABLE` in `.env`).

## Run signals and whys

After `.env` has your CRM Power BI workspace/dataset IDs:

```powershell
cd ms-insights-portal-crm
pip install -e .
$env:ONEPLATFORM_SERVICE__PORT = "8014"
python main.py
```

Swagger: http://localhost:8014/docs → **PBI - Trigger**

| Step | Endpoint |
|------|----------|
| Signals (all active jobs) | `POST /pbi/trigger/signals` |
| Signals (one KPI) | `POST /pbi/trigger/signals/{kpi_name}` |
| Whys (unprocessed) | `POST /pbi/trigger/whys` |
| Full pipeline | `POST /pbi/trigger/full-insights-pipeline` |

Example — run signals for one job id:

```json
POST /pbi/trigger/signals
{
  "job_ids": [42],
  "start_date": "2025-01-01",
  "end_date": "2025-01-31"
}
```

Then run whys (omit body to process all unprocessed signals):

```json
POST /pbi/trigger/whys
{}
```

## Portfolio-level KPI value (`SELF`)

Fetch the **raw KPI total** for the job date window with **no business dimension** drill.
Thresholds (e.g. trigger when value `gt` 500) are defined on the **signal**, not in the feature.

### 1. Register the feature (`config_featuresrenuitycrm`)

```sql
INSERT INTO insights.config_featuresrenuitycrm (
  feature_name, function_name, label, description,
  requires_time_dimension, default_params, format
) VALUES (
  'self_kpi_value',
  'calculate_self_kpi_value',
  'Portfolio KPI value',
  'Raw KPI total for the job period (no dimension drill)',
  false,
  '{}',
  'absolute'
)
ON CONFLICT (feature_name) DO UPDATE SET
  function_name = EXCLUDED.function_name,
  label = EXCLUDED.label,
  description = EXCLUDED.description,
  format = EXCLUDED.format;
```

### 2. Signal definition (`config_signalsrenuitycrm`)

Example — fire when KPI total is above 500:

```sql
-- operator: lt | gt | lte | gte | between
-- format: absolute (threshold is raw KPI units, not %)
INSERT INTO insights.config_signalsrenuitycrm (
  signal_name, feature_name, operator, threshold, threshold2, format, ...
) VALUES (
  'my_kpi_above_500',
  'self_kpi_value',
  'gt',
  500,
  NULL,
  'absolute',
  ...
);
```

### 3. Signal job (when you are ready)

Use dimension **`SELF`** in `config_signaljobsrenuitycrm.dimensions` (no `config_dimensions` row needed):

```json
{
  "dimensions": ["SELF"],
  "features": ["self_kpi_value"],
  "signals": ["my_kpi_above_500"]
}
```

Signals are stored with `dimension_value = ALL`. Job filters (e.g. market, channel) still apply via `filters` on the job row.
