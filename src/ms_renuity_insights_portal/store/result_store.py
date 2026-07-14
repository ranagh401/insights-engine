"""PostgreSQL result store for the DAX KPI engine.

Manages operational tables including kpi_results, signal_log, why_results,
``main_insights`` (main insight narratives), and KPI correlation priors
for narrative reasoning support, using SQLAlchemy async.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional
from uuid import UUID as PyUUID
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    delete,
    desc,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..config.models import (
    KPIRow,
    Signal,
    WhyAnalysisResult,
    WhyRow,
    MainInsight,
)
from ..models.schema import SCHEMA

logger = logging.getLogger(__name__)


def _as_pg_uuid(val: str | PyUUID) -> PyUUID:
    if isinstance(val, PyUUID):
        return val
    return PyUUID(str(val).strip())

# Physical Postgres table for narrative rows (API/view still use ``vw_main_insights``).
MAIN_INSIGHTS_CRM_TABLE = "maininsightscrm"
MAIN_INSIGHTS_DEV_TABLE = "main_insightsdev"
MAIN_INSIGHTS_LEGACY_TABLE = "main_insights"
MAIN_INSIGHTS_DEFAULT_TABLE = MAIN_INSIGHTS_CRM_TABLE
MAIN_INSIGHTS_ALLOWED_TABLES = frozenset(
    {
        MAIN_INSIGHTS_CRM_TABLE,
        MAIN_INSIGHTS_DEV_TABLE,
        MAIN_INSIGHTS_LEGACY_TABLE,
    }
)
# Backward-compatible alias (historically used by this module as the default table).
MAIN_INSIGHTS_PHYSICAL_TABLE = MAIN_INSIGHTS_DEFAULT_TABLE

MAIN_INSIGHTS_TABLE_QUERY_HELP = (
    f"Optional target table: {SCHEMA}.{MAIN_INSIGHTS_CRM_TABLE} (CRM default), "
    f"{SCHEMA}.{MAIN_INSIGHTS_DEV_TABLE}, or {SCHEMA}.{MAIN_INSIGHTS_LEGACY_TABLE}."
)


def normalize_main_insights_table_name(table_name: str | None) -> str:
    """Normalize API/store input into a supported physical table name.

    Accepted values:
    - ``maininsightscrm`` / ``main_insights`` / ``main_insightsdev``
    - ``insights.<table>`` variants
    - ``None`` / empty -> CRM default ``maininsightscrm``
    """
    raw = (table_name or "").strip().lower()
    if raw == "":
        return MAIN_INSIGHTS_DEFAULT_TABLE
    if raw.startswith(f"{SCHEMA}."):
        raw = raw.split(".", 1)[1].strip()
    if raw in MAIN_INSIGHTS_ALLOWED_TABLES:
        return raw
    allowed = ", ".join(f"{SCHEMA}.{t}" for t in sorted(MAIN_INSIGHTS_ALLOWED_TABLES))
    raise ValueError(
        f"Invalid main insights table '{table_name}'. Allowed values: {allowed}"
    )

# asyncpg enforces PostgreSQL's ~32767 bind-parameter limit per prepared statement.
# Large ``IN (...)`` lists must be split into smaller batches.
_PG_IN_CLAUSE_BATCH = 8000

# Bulk INSERT batch size for ``main_insights`` (row width is large; keep under asyncpg param limits).
_MAIN_INSIGHTS_INSERT_BATCH = 100


def _mapping_to_jsonable(row: Any) -> dict[str, Any]:
    """Turn a SQLAlchemy RowMapping into JSON-serializable primitives."""
    out: dict[str, Any] = {}
    for key, val in dict(row).items():
        if val is None:
            out[key] = None
        elif isinstance(val, uuid.UUID):
            out[key] = str(val)
        elif isinstance(val, (datetime, date)):
            out[key] = val.isoformat()
        elif isinstance(val, Decimal):
            out[key] = float(val)
        else:
            out[key] = val
    return out


def _main_insight_view_ddl(schema: str, table_name: str = MAIN_INSIGHTS_DEFAULT_TABLE) -> str:
    """``CREATE VIEW`` body for vw_main_insights (PascalCase; use after ``DROP VIEW IF EXISTS``).

    PostgreSQL ``CREATE OR REPLACE VIEW`` cannot remove columns from an existing view; a prior
    definition with extra columns would raise *cannot drop columns from view*. Drop then create.
    """
    return f"""
CREATE VIEW {schema}.vw_main_insights AS
SELECT
  m.insight_id::text AS "InsightID",
  m.run_timestamp AS "RunTimestamp",
  m.signal_ids AS "SignalIDs",
  m.kpi_family AS "KPIFamily",
  m.title AS "Title",
  m.kpi AS "KPI",
  m.dimension_name AS "DimensionName",
  m.dimension_value AS "DimensionValue",
  m.insight AS "Insight",
  m.why AS "Why",
  m.period AS "Period",
  m.period_start AS "PeriodStart",
  m.period_end AS "PeriodEnd",
  m.insight_summary AS "InsightSummary",
  m.why_insight_summary AS "WhyInsightSummary",
  m.severity AS "Severity",
  m.tags AS "Tags",
  m.impact_insight AS "ImpactInsight",
  (CASE WHEN m.park THEN 1 ELSE 0 END) AS "Park",
  m.created_at AS "CreatedAt",
  m.hook AS "Hook",
  m."like" AS "Like",
  m."dislike" AS "Dislike",
  m.remarks AS "Remarks",
  m.recommended_actions AS "RecommendedActions"
FROM {schema}.{table_name} m;
"""


def _main_insight_row_to_pascal(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map a ``main_insights`` row to the same shape as ``vw_main_insights`` (JSON-friendly)."""
    d = dict(row)
    park = d.get("park")

    def _iso(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        return v

    iid = d.get("insight_id")
    if iid is not None and not isinstance(iid, str):
        iid = str(iid)

    return {
        "InsightID": iid,
        "RunTimestamp": _iso(d.get("run_timestamp")),
        "SignalIDs": d.get("signal_ids"),
        "KPIFamily": d.get("kpi_family"),
        "Title": d.get("title"),
        "KPI": d.get("kpi"),
        "DimensionName": d.get("dimension_name"),
        "DimensionValue": d.get("dimension_value"),
        "Insight": d.get("insight"),
        "Why": d.get("why"),
        "Period": d.get("period"),
        "PeriodStart": _iso(d.get("period_start")),
        "PeriodEnd": _iso(d.get("period_end")),
        "InsightSummary": d.get("insight_summary"),
        "WhyInsightSummary": d.get("why_insight_summary"),
        "Severity": d.get("severity"),
        "Tags": d.get("tags"),
        "ImpactInsight": d.get("impact_insight"),
        "Park": 1 if park is True else 0,
        "CreatedAt": _iso(d.get("created_at")),
        "Hook": d.get("hook"),
        "Like": d.get("like"),
        "Dislike": d.get("dislike"),
        "Remarks": d.get("remarks"),
        "RecommendedActions": d.get("recommended_actions"),
    }


metadata = MetaData(schema=SCHEMA)

kpi_results_table = Table(
    "kpi_results",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("kpi_name", String, nullable=False),
    Column("grain", String, nullable=False),
    Column("dimension_value", String, nullable=False),
    Column("kpi_value", Numeric, nullable=True),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("job_id", String, nullable=True),
    Column("result_type", String, server_default="kpi"),
    Column("dependency_kpi_name", String, nullable=True),
    Column("computed_at", DateTime(timezone=True), server_default=text("now()")),
    UniqueConstraint(
        "kpi_name",
        "grain",
        "dimension_value",
        "period_start",
        "period_end",
        "result_type",
        name="uq_kpi_results_grain",
    ),
    Index("ix_kpi_results_lookup", "kpi_name", "grain", "period_start", "period_end"),
    Index(
        "ix_kpi_results_entity",
        "kpi_name",
        "grain",
        "dimension_value",
        "period_start",
    ),
)

signal_log_table = Table(
    "signal_log",
    metadata,
    Column(
        "signal_id", UUID, primary_key=True, server_default=text("gen_random_uuid()")
    ),
    Column("kpi_name", String, nullable=False),
    Column("dimension", String, nullable=False),
    Column("dimension_value", String, nullable=False),
    Column("signal_name", String, nullable=True),
    Column("feature_name", String, nullable=True),
    Column("feature_value", Numeric, nullable=True),
    Column("current_kpi_value", Numeric, nullable=True),
    Column("prev_kpi_value", Numeric, nullable=True),
    Column("observed_value", Numeric, nullable=False),
    Column("threshold_value", Numeric, nullable=False),
    Column("operator", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("breach_delta", Numeric, nullable=False),
    Column("detected_at", DateTime(timezone=True), server_default=text("now()")),
    Column("why_computed", Boolean, server_default=text("false")),
    Column("job_id", String, nullable=True),
    Column("dax_kpi_query", String, nullable=True),
    Column("dax_feature_query", String, nullable=True),
    Index("ix_signal_log_kpi_dim", "kpi_name", "dimension", "detected_at"),
    Index("ix_signal_log_unprocessed", "why_computed", "detected_at"),
)

why_results_table = Table(
    "why_results",
    metadata,
    Column("why_id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("signal_id", UUID, nullable=False),
    Column("run_timestamp", DateTime(timezone=True), server_default=text("now()")),
    Column("kpi_name", String, nullable=False),
    Column("dimension_name", String, nullable=False),
    Column("dimension_value", String, nullable=False),
    Column("signal_name", String, nullable=True),
    Column("dep_kpi_name", String, nullable=True),
    Column("dep_kpi_label", String, nullable=True),
    Column("rationale", String, nullable=True),
    Column("current_value", Numeric, nullable=True),
    Column("prev_value", Numeric, nullable=True),
    Column("change_pct", Numeric, nullable=True),
    Column("period", String, nullable=True),
    Column("period_start", Date, nullable=True),
    Column("period_end", Date, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    Index("ix_why_results_signal_id", "signal_id"),
)

def _build_main_insights_table(table_name: str, run_idx_name: str) -> Table:
    # Column order matches frontend grid: InsightID … Hook (see vw_main_insights view for PascalCase).
    return Table(
        table_name,
        metadata,
        Column("insight_id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
        Column("run_timestamp", DateTime(timezone=True), nullable=False),
        Column("signal_ids", Text, nullable=False),
        Column("kpi_family", Text, nullable=True),
        Column("title", Text, nullable=True),
        Column("kpi", Text, nullable=True),
        Column("dimension_name", Text, nullable=True),
        Column("dimension_value", Text, nullable=True),
        Column("insight", Text, nullable=False),
        Column("why", Text, nullable=True),
        Column("period", Text, nullable=True),
        Column("period_start", Date, nullable=True),
        Column("period_end", Date, nullable=True),
        Column("insight_summary", Text, nullable=True),
        Column("why_insight_summary", Text, nullable=True),
        Column("severity", Text, nullable=True),
        Column("tags", Text, nullable=True),
        Column("impact_insight", Text, nullable=True),
        Column("park", Boolean, nullable=False, server_default=text("false")),
        Column("created_at", DateTime(timezone=True), server_default=text("now()")),
        Column("hook", Text, nullable=True),
        Column("like", Boolean, nullable=True),
        Column("dislike", Boolean, nullable=True),
        Column("remarks", Text, nullable=True),
        Column("recommended_actions", Text, nullable=True),
        # Manual KPI-card group tag (e.g. group1..group5) for the executive summary.
        Column("group_name", Text, nullable=True),
        Index(run_idx_name, "run_timestamp"),
    )


maininsightscrm_table = _build_main_insights_table(
    MAIN_INSIGHTS_CRM_TABLE, "ix_maininsightscrm_run"
)
main_insights_table = _build_main_insights_table(
    MAIN_INSIGHTS_LEGACY_TABLE, "ix_main_insights_run"
)

# Precomputed executive summary per KPI-card group (avoids an LLM call per request).
finalcrm_table = Table(
    "finalcrm",
    metadata,
    Column("group_name", Text, primary_key=True),
    Column("executive_summary", JSONB, nullable=True),
    Column("insight", JSONB, nullable=True),
    Column("recommended_action", Text, nullable=True),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()")),
)
main_insightsdev_table = _build_main_insights_table(
    MAIN_INSIGHTS_DEV_TABLE, "ix_main_insightsdev_run"
)
MAIN_INSIGHTS_TABLE_MAP: dict[str, Table] = {
    MAIN_INSIGHTS_CRM_TABLE: maininsightscrm_table,
    MAIN_INSIGHTS_LEGACY_TABLE: main_insights_table,
    MAIN_INSIGHTS_DEV_TABLE: main_insightsdev_table,
}

correlation_priors_table = Table(
    "kpi_correlation_priors",
    metadata,
    Column("prior_id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("target_kpi", Text, nullable=False),
    Column("regressor", Text, nullable=False),
    Column("coef", Numeric, nullable=True),
    Column("p_value", Numeric, nullable=True),
    Column("business_interpretation", Text, nullable=True),
    Column("model_version", Text, nullable=False, server_default=text("''")),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()")),
    UniqueConstraint("target_kpi", "regressor", "model_version", name="uq_corr_priors_key"),
    Index("ix_corr_priors_target", "target_kpi"),
    Index("ix_corr_priors_active_target", "is_active", "target_kpi"),
)

anomaly_timeline_table = Table(
    "kpi_anomaly_timeline",
    metadata,
    Column("anomaly_id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("kpi_name", Text, nullable=False),
    Column("week_start_date", Date, nullable=False),
    Column("kpi_value", Numeric, nullable=True),
    Column("anomaly_score", Numeric, nullable=True),
    Column("anomaly_flag", Integer, nullable=False),
    Column("top_driver", Text, nullable=True),
    Column("dimension_name", Text, nullable=False, server_default=text("''")),
    Column("dimension_value", Text, nullable=False, server_default=text("''")),
    Column("model_version", Text, nullable=False, server_default=text("''")),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), server_default=text("now()")),
    UniqueConstraint(
        "kpi_name",
        "week_start_date",
        "dimension_name",
        "dimension_value",
        "model_version",
        name="uq_anomaly_timeline_key",
    ),
    Index("ix_anomaly_timeline_kpi_week", "kpi_name", "week_start_date"),
    Index("ix_anomaly_timeline_active_kpi", "is_active", "kpi_name"),
)


def _signal_from_signal_log_row(r: Mapping[str, Any]) -> Signal:
    """Map a ``signal_log`` row to a ``Signal`` model."""
    return Signal(
        signal_id=str(r["signal_id"]),
        kpi_name=r["kpi_name"],
        dimension=r["dimension"],
        dimension_value=r["dimension_value"],
        signal_name=r.get("signal_name") or "",
        feature_name=r.get("feature_name") or "",
        feature_value=float(r["feature_value"]) if r.get("feature_value") is not None else 0.0,
        current_kpi_value=float(r["current_kpi_value"]) if r.get("current_kpi_value") is not None else None,
        prev_kpi_value=float(r["prev_kpi_value"]) if r.get("prev_kpi_value") is not None else None,
        observed_value=float(r["observed_value"]),
        threshold_value=float(r["threshold_value"]),
        operator=r["operator"],
        severity=r["severity"],
        breach_delta=float(r["breach_delta"]),
        detected_at=r["detected_at"],
        why_computed=r["why_computed"],
        job_id=r["job_id"] or "",
        dax_kpi_query=r.get("dax_kpi_query"),
        dax_feature_query=r.get("dax_feature_query"),
    )


class ResultStore:
    """Async PostgreSQL store for KPI results, signals, and WHY analysis."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker,
        main_insights_table_name: str = MAIN_INSIGHTS_DEFAULT_TABLE,
    ) -> None:
        self._engine = engine
        self._sf = session_factory
        self._main_insights_table_name = normalize_main_insights_table_name(
            main_insights_table_name
        )

    @property
    def main_insights_table_name(self) -> str:
        return self._main_insights_table_name

    def with_main_insights_table(self, table_name: str | None) -> "ResultStore":
        """Return a lightweight store clone targeting a specific main-insights table."""
        return ResultStore(
            self._engine,
            self._sf,
            normalize_main_insights_table_name(table_name),
        )

    def _main_insights_table(self) -> Table:
        return MAIN_INSIGHTS_TABLE_MAP[self._main_insights_table_name]

    async def ensure_tables(self) -> None:
        """Create core tables, then run best-effort DDL each in its own transaction.

        PostgreSQL/asyncpg: a failed statement aborts the *entire* transaction; nested
        savepoints do not always recover cleanly across many DDL steps. One
        ``engine.begin()`` per optional statement avoids ``RELEASE SAVEPOINT`` /
        ``InFailedSQLTransactionError`` cascades.
        """
        async with self._engine.begin() as conn:
            # Drop the old why_results table if it has the legacy schema
            # (check for the old 'id' PK column which was renamed to 'why_id').
            async with conn.begin_nested():
                try:
                    row = await conn.execute(text(
                        f"SELECT column_name FROM information_schema.columns "
                        f"WHERE table_schema = '{SCHEMA}' AND table_name = 'why_results' "
                        f"AND column_name = 'id'"
                    ))
                    if row.first() is not None:
                        logger.info("Dropping legacy why_results table (old schema detected).")
                        await conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.why_results"))
                except Exception:
                    logger.debug("ensure_tables: legacy why_results check skipped", exc_info=True)

            await conn.run_sync(metadata.create_all)

        _new_cols = [
            ("signal_log", "signal_name", "VARCHAR"),
            ("signal_log", "feature_name", "VARCHAR"),
            ("signal_log", "feature_value", "NUMERIC"),
            ("signal_log", "current_kpi_value", "NUMERIC"),
            ("signal_log", "prev_kpi_value", "NUMERIC"),
            ("signal_log", "dax_kpi_query", "TEXT"),
            ("signal_log", "dax_feature_query", "TEXT"),
        ]
        for tbl, col, dtype in _new_cols:
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(text(
                        f"ALTER TABLE {SCHEMA}.{tbl} ADD COLUMN IF NOT EXISTS {col} {dtype}"
                    ))
            except Exception:
                logger.debug(
                    "ensure_tables: skip ALTER %s.%s (%s)", SCHEMA, tbl, col, exc_info=True
                )

        _main_insights_cols: list[tuple[str, str, str]] = []
        for _mi_tbl in MAIN_INSIGHTS_ALLOWED_TABLES:
            _main_insights_cols.extend(
                [
                    (_mi_tbl, "park", "BOOLEAN DEFAULT false"),
                    (_mi_tbl, "hook", "TEXT"),
                    (_mi_tbl, '"like"', "BOOLEAN"),
                    (_mi_tbl, '"dislike"', "BOOLEAN"),
                    (_mi_tbl, "remarks", "TEXT"),
                    (_mi_tbl, "recommended_actions", "TEXT"),
                    (_mi_tbl, "period_start", "DATE"),
                    (_mi_tbl, "period_end", "DATE"),
                ]
            )
        for tbl, col, dtype in _main_insights_cols:
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(text(
                        f"ALTER TABLE {SCHEMA}.{tbl} ADD COLUMN IF NOT EXISTS {col} {dtype}"
                    ))
            except Exception:
                logger.debug(
                    "ensure_tables: skip ALTER %s.%s (%s)", SCHEMA, tbl, col, exc_info=True
                )

        for _mi_tbl in MAIN_INSIGHTS_ALLOWED_TABLES:
            for col in (
                "insight",
                "why",
                "title",
                "kpi_family",
                "kpi",
                "dimension_name",
                "dimension_value",
                "signal_ids",
                "insight_summary",
                "why_insight_summary",
                "period",
                "severity",
                "tags",
                "impact_insight",
                "hook",
            ):
                try:
                    async with self._engine.begin() as conn:
                        await conn.execute(text(
                            f"ALTER TABLE {SCHEMA}.{_mi_tbl} "
                            f"ALTER COLUMN {col} TYPE TEXT USING {col}::text"
                        ))
                except Exception:
                    logger.debug(
                        "ensure_tables: skip ALTER %s.%s", _mi_tbl, col, exc_info=True
                    )

        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(f"DROP VIEW IF EXISTS {SCHEMA}.vw_main_insights")
                )
                await conn.execute(
                    text(_main_insight_view_ddl(SCHEMA, MAIN_INSIGHTS_DEFAULT_TABLE))
                )
        except Exception:
            logger.warning(
                "Could not refresh vw_main_insights view (schema drift or permissions).",
                exc_info=True,
            )

        # Legacy column removed from product; drop after view refresh so nothing references it.
        for _mi_tbl in MAIN_INSIGHTS_ALLOWED_TABLES:
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(text(
                        f"ALTER TABLE {SCHEMA}.{_mi_tbl} "
                        f"DROP COLUMN IF EXISTS whys_rephrase"
                    ))
            except Exception:
                logger.debug(
                    "ensure_tables: skip DROP whys_rephrase on %s",
                    _mi_tbl,
                    exc_info=True,
                )

        logger.info("Result-store tables verified/created.")

    async def write_kpi_results(
        self, rows: list[KPIRow], job_id: str
    ) -> None:
        if not rows:
            return
        async with self._sf() as session:
            for row in rows:
                dim_val = next(iter(row.dimension_values.values()), "")
                stmt = insert(kpi_results_table).values(
                    id=uuid4(),
                    kpi_name=row.kpi_name,
                    grain=row.grain,
                    dimension_value=dim_val,
                    kpi_value=row.kpi_value,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    job_id=job_id,
                    result_type=row.result_type,
                    dependency_kpi_name=row.dependency_kpi_name,
                    computed_at=row.computed_at,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_kpi_results_grain",
                    set_={
                        "kpi_value": stmt.excluded.kpi_value,
                        "job_id": stmt.excluded.job_id,
                        "computed_at": stmt.excluded.computed_at,
                        "dependency_kpi_name": stmt.excluded.dependency_kpi_name,
                    },
                )
                await session.execute(stmt)
            await session.commit()

    async def get_kpi_results(
        self,
        kpi_name: str,
        grain: str,
        period_start: date,
        period_end: date,
    ) -> list[KPIRow]:
        async with self._sf() as session:
            stmt = select(kpi_results_table).where(
                and_(
                    kpi_results_table.c.kpi_name == kpi_name,
                    kpi_results_table.c.grain == grain,
                    kpi_results_table.c.period_start == period_start,
                    kpi_results_table.c.period_end == period_end,
                )
            )
            result = await session.execute(stmt)
            db_rows = result.mappings().all()

        return [
            KPIRow(
                dimension_values={grain: r["dimension_value"]},
                kpi_name=r["kpi_name"],
                kpi_value=float(r["kpi_value"]) if r["kpi_value"] is not None else None,
                grain=r["grain"],
                period_start=r["period_start"],
                period_end=r["period_end"],
                computed_at=r["computed_at"],
                result_type=r["result_type"] or "kpi",
                dependency_kpi_name=r.get("dependency_kpi_name"),
            )
            for r in db_rows
        ]

    async def write_signals(
        self, signals: list[Signal], job_id: str
    ) -> None:
        if not signals:
            return

        def _row(sig: Signal) -> dict[str, Any]:
            return {
                "signal_id": sig.signal_id,
                "kpi_name": sig.kpi_name,
                "dimension": sig.dimension,
                "dimension_value": sig.dimension_value,
                "signal_name": sig.signal_name,
                "feature_name": sig.feature_name,
                "feature_value": sig.feature_value,
                "current_kpi_value": sig.current_kpi_value,
                "prev_kpi_value": sig.prev_kpi_value,
                "observed_value": sig.observed_value,
                "threshold_value": sig.threshold_value,
                "operator": sig.operator,
                "severity": sig.severity,
                "breach_delta": sig.breach_delta,
                "detected_at": sig.detected_at,
                "why_computed": sig.why_computed,
                "job_id": job_id,
                "dax_kpi_query": sig.dax_kpi_query,
                "dax_feature_query": sig.dax_feature_query,
            }

        rows = [_row(sig) for sig in signals]
        chunk = 400
        async with self._sf() as session:
            for i in range(0, len(rows), chunk):
                batch = rows[i : i + chunk]
                await session.execute(insert(signal_log_table), batch)
            await session.commit()

    async def count_unprocessed_signals(self) -> int:
        """Count rows with ``why_computed = false`` (cheap index-friendly query)."""
        async with self._sf() as session:
            stmt = (
                select(func.count())
                .select_from(signal_log_table)
                .where(signal_log_table.c.why_computed.is_(False))
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def get_unprocessed_signals(
        self,
        *,
        limit: int | None = None,
        order_detected_at: str | None = None,
    ) -> list[Signal]:
        """Load unprocessed signals; optional ``limit`` + ``order_detected_at`` (``asc``/``desc``).

        WHY sweeps use ``limit`` + ``asc`` so work is done in bounded DB chunks (FIFO by
        ``detected_at``), similar to batching in the Databricks ``compute_whys`` path.
        """
        async with self._sf() as session:
            stmt = select(signal_log_table).where(
                signal_log_table.c.why_computed.is_(False)
            )
            if order_detected_at:
                o = order_detected_at.strip().lower()
                if o == "desc":
                    stmt = stmt.order_by(desc(signal_log_table.c.detected_at))
                else:
                    stmt = stmt.order_by(signal_log_table.c.detected_at.asc())
            if limit is not None and limit > 0:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            db_rows = result.mappings().all()

        return [_signal_from_signal_log_row(r) for r in db_rows]

    async def get_unprocessed_signal_by_id(self, signal_id: str) -> Signal | None:
        """Return one unprocessed signal by id, or ``None`` (avoids loading the full queue)."""
        try:
            sid = PyUUID(str(signal_id).strip())
        except (ValueError, AttributeError):
            return None
        async with self._sf() as session:
            stmt = select(signal_log_table).where(
                signal_log_table.c.signal_id == sid,
                signal_log_table.c.why_computed.is_(False),
            )
            result = await session.execute(stmt)
            r = result.mappings().first()
        return _signal_from_signal_log_row(r) if r else None

    async def list_distinct_signal_ids_for_kpi_names(
        self,
        kpi_names: list[str],
        *,
        only_unprocessed: bool = False,
    ) -> list[str]:
        """Distinct ``signal_log.signal_id`` rows whose ``kpi_name`` matches any given name (case-insensitive).

        Use after dependency-config changes to re-run WHY for affected parent KPIs only.
        When ``only_unprocessed`` is true, restricts to rows with ``why_computed = false``.
        """
        keys = {(n or "").strip().lower() for n in (kpi_names or []) if (n or "").strip()}
        if not keys:
            return []
        async with self._sf() as session:
            cond = func.lower(signal_log_table.c.kpi_name).in_(sorted(keys))
            if only_unprocessed:
                cond = and_(cond, signal_log_table.c.why_computed.is_(False))
            stmt = select(signal_log_table.c.signal_id).where(cond).distinct()
            result = await session.execute(stmt)
            ordered: list[str] = []
            seen: set[str] = set()
            for (sid,) in result.all():
                s = str(sid)
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
            return ordered

    async def list_config_kpi_names(self) -> tuple[str, ...]:
        """All ``kpiname`` values from insights.configkpisrenuitycrm (portal KPI allow-list)."""
        from ..models.config_kpi import ConfigKPI

        async with self._sf() as session:
            stmt = select(ConfigKPI.kpi_name).order_by(ConfigKPI.kpi_name)
            result = await session.execute(stmt)
            return tuple(row[0] for row in result.all() if row[0])

    async def write_why_results(
        self,
        why_rows: list[WhyRow],
    ) -> None:
        if not why_rows:
            return
        rows = []
        for wr in why_rows:
            try:
                wid = _as_pg_uuid(wr.why_id) if wr.why_id else uuid4()
                sid = _as_pg_uuid(wr.signal_id)
            except (ValueError, TypeError) as exc:
                logger.error(
                    "write_why_results | skip row — invalid UUID (why_id=%r signal_id=%r): %s",
                    wr.why_id,
                    wr.signal_id,
                    exc,
                )
                continue
            rows.append(
                {
                    "why_id": wid,
                    "signal_id": sid,
                    "run_timestamp": wr.run_timestamp,
                    "kpi_name": wr.kpi_name,
                    "dimension_name": wr.dimension_name,
                    "dimension_value": wr.dimension_value,
                    "signal_name": wr.signal_name,
                    "dep_kpi_name": wr.dep_kpi_name,
                    "dep_kpi_label": wr.dep_kpi_label,
                    "rationale": wr.rationale,
                    "current_value": wr.current_value,
                    "prev_value": wr.prev_value,
                    "change_pct": wr.change_pct,
                    "period": wr.period,
                    "period_start": wr.period_start,
                    "period_end": wr.period_end,
                }
            )
        if not rows:
            logger.warning(
                "write_why_results | all %d input row(s) dropped (UUID coercion failed)",
                len(why_rows),
            )
            return
        chunk = 500
        try:
            async with self._sf() as session:
                for i in range(0, len(rows), chunk):
                    await session.execute(insert(why_results_table), rows[i : i + chunk])
                await session.commit()
        except Exception:
            logger.exception(
                "write_why_results | INSERT into %s.why_results failed (%d row(s))",
                SCHEMA,
                len(rows),
            )
            raise
        logger.info(
            "write_why_results | committed %d row(s) to %s.why_results",
            len(rows),
            SCHEMA,
        )

    async def mark_why_computed(self, signal_id: str) -> None:
        async with self._sf() as session:
            stmt = (
                update(signal_log_table)
                .where(signal_log_table.c.signal_id == signal_id)
                .values(why_computed=True)
            )
            await session.execute(stmt)
            await session.commit()

    async def mark_why_computed_bulk(self, signal_ids: list[str]) -> None:
        """Mark multiple signals as why_computed=True in one round-trip."""
        if not signal_ids:
            return
        chunk = 500
        async with self._sf() as session:
            for i in range(0, len(signal_ids), chunk):
                batch = signal_ids[i : i + chunk]
                stmt = (
                    update(signal_log_table)
                    .where(signal_log_table.c.signal_id.in_(batch))
                    .values(why_computed=True)
                )
                await session.execute(stmt)
            await session.commit()

    async def reset_why_computed_for_signal_ids(self, signal_ids: list[str]) -> int:
        """Set ``why_computed = false`` for the given signal IDs (for targeted WHY re-runs)."""
        if not signal_ids:
            return 0
        uuids: list[PyUUID] = []
        for sid in signal_ids:
            try:
                uuids.append(_as_pg_uuid(sid))
            except (ValueError, TypeError):
                continue
        if not uuids:
            return 0
        total = 0
        chunk = _PG_IN_CLAUSE_BATCH
        async with self._sf() as session:
            for i in range(0, len(uuids), chunk):
                batch = uuids[i : i + chunk]
                stmt = (
                    update(signal_log_table)
                    .where(signal_log_table.c.signal_id.in_(batch))
                    .values(why_computed=False)
                )
                r = await session.execute(stmt)
                total += int(r.rowcount or 0)
            await session.commit()
        return total

    async def delete_why_results_for_signal_ids(self, signal_ids: list[str]) -> int:
        """Remove existing WHY rows for the given signals (avoid duplicates on re-run)."""
        if not signal_ids:
            return 0
        uuids: list[PyUUID] = []
        for sid in signal_ids:
            try:
                uuids.append(_as_pg_uuid(sid))
            except (ValueError, TypeError):
                continue
        if not uuids:
            return 0
        total = 0
        chunk = _PG_IN_CLAUSE_BATCH
        async with self._sf() as session:
            for i in range(0, len(uuids), chunk):
                batch = uuids[i : i + chunk]
                stmt = delete(why_results_table).where(why_results_table.c.signal_id.in_(batch))
                r = await session.execute(stmt)
                total += int(r.rowcount or 0)
            await session.commit()
        logger.info(
            "delete_why_results_for_signal_ids | removed %d row(s) across %d signal id(s)",
            total,
            len(uuids),
        )
        return total

    async def get_why_results(self, signal_id: str) -> WhyAnalysisResult | None:
        async with self._sf() as session:
            sig_stmt = select(signal_log_table).where(
                signal_log_table.c.signal_id == signal_id
            )
            sig_result = await session.execute(sig_stmt)
            sig_row = sig_result.mappings().first()
            if sig_row is None:
                return None

            signal = Signal(
                signal_id=str(sig_row["signal_id"]),
                kpi_name=sig_row["kpi_name"],
                dimension=sig_row["dimension"],
                dimension_value=sig_row["dimension_value"],
                signal_name=sig_row.get("signal_name") or "",
                feature_name=sig_row.get("feature_name") or "",
                feature_value=float(sig_row["feature_value"]) if sig_row.get("feature_value") is not None else 0.0,
                current_kpi_value=float(sig_row["current_kpi_value"]) if sig_row.get("current_kpi_value") is not None else None,
                prev_kpi_value=float(sig_row["prev_kpi_value"]) if sig_row.get("prev_kpi_value") is not None else None,
                observed_value=float(sig_row["observed_value"]),
                threshold_value=float(sig_row["threshold_value"]),
                operator=sig_row["operator"],
                severity=sig_row["severity"],
                breach_delta=float(sig_row["breach_delta"]),
                detected_at=sig_row["detected_at"],
                why_computed=sig_row["why_computed"],
                job_id=sig_row["job_id"] or "",
                dax_kpi_query=sig_row.get("dax_kpi_query"),
                dax_feature_query=sig_row.get("dax_feature_query"),
            )

            why_stmt = select(why_results_table).where(
                why_results_table.c.signal_id == signal_id
            )
            why_result = await session.execute(why_stmt)
            db_rows = why_result.mappings().all()

        rows = [
            WhyRow(
                why_id=str(r["why_id"]),
                signal_id=str(r["signal_id"]),
                run_timestamp=r["run_timestamp"],
                kpi_name=r["kpi_name"],
                dimension_name=r["dimension_name"],
                dimension_value=r["dimension_value"],
                signal_name=r.get("signal_name") or "",
                dep_kpi_name=r.get("dep_kpi_name"),
                dep_kpi_label=r.get("dep_kpi_label"),
                rationale=r.get("rationale") or "",
                current_value=float(r["current_value"]) if r.get("current_value") is not None else None,
                prev_value=float(r["prev_value"]) if r.get("prev_value") is not None else None,
                change_pct=float(r["change_pct"]) if r.get("change_pct") is not None else None,
                period=r.get("period") or "",
                period_start=r.get("period_start"),
                period_end=r.get("period_end"),
                created_at=r.get("created_at"),
            )
            for r in db_rows
        ]

        return WhyAnalysisResult(
            signal=signal,
            why_rows=rows,
            analysis_timestamp=datetime.now(timezone.utc),
        )

    async def get_latest_signal_timestamp(self) -> datetime | None:
        async with self._sf() as session:
            stmt = select(signal_log_table.c.detected_at).order_by(signal_log_table.c.detected_at.desc()).limit(1)
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    async def get_max_signal_detected_at(self) -> datetime | None:
        """Maximum ``detected_at`` over ``signal_log`` (for dimensional run batch label)."""
        async with self._sf() as session:
            stmt = select(func.max(signal_log_table.c.detected_at))
            result = await session.execute(stmt)
            return result.scalar()

    async def fetch_dimensional_buckets_json_agg(
        self,
        *,
        target_dimensions_lower: list[str],
        all_timestamps: bool,
        batch_ts: datetime | None,
    ) -> list[dict[str, Any]]:
        """One row per ``(dimension, dimension_value, period_start, period_end)`` with JSON aggregates.

        Mirrors dimensional clustering: join ``why_results`` → ``signal_log``, filter slice dimensions,
        require non-null WHY period bounds, optionally restrict ``signal_log`` to ``batch_ts``.

        JSON shapes match the dimensional aggregation pattern::

            json_agg(t) FROM (SELECT <subset of signal_log columns> ...) t
            json_agg(t) FROM (SELECT <subset of why_results columns> ...) t

        (same column lists as the product SQL examples — not ``row_to_json(table.*)``).
        """
        if not target_dimensions_lower:
            return []
        dims = [d.strip().lower() for d in target_dimensions_lower if (d or "").strip()]
        if not dims:
            return []

        q = text(
            f"""
            WITH bucket_keys AS (
              SELECT
                trim(s.dimension) AS dimension_name,
                trim(s.dimension_value) AS dimension_value,
                w.period_start,
                w.period_end,
                array_agg(DISTINCT w.why_id) FILTER (WHERE w.why_id IS NOT NULL) AS why_ids,
                array_agg(DISTINCT s.signal_id) FILTER (WHERE s.signal_id IS NOT NULL) AS signal_ids
              FROM {SCHEMA}.why_results w
              INNER JOIN {SCHEMA}.signal_log s ON w.signal_id = s.signal_id
              WHERE w.period_start IS NOT NULL
                AND w.period_end IS NOT NULL
                AND lower(trim(s.dimension)) = ANY(CAST(:dims_lower AS text[]))
                AND (
                  :all_timestamps
                  OR s.detected_at = :batch_ts
                )
              GROUP BY trim(s.dimension), trim(s.dimension_value), w.period_start, w.period_end
            )
            SELECT
              bk.dimension_name,
              bk.dimension_value,
              bk.period_start,
              bk.period_end,
              (
                SELECT COALESCE(json_agg(t), '[]'::json)
                FROM (
                  SELECT
                    sl.signal_id,
                    sl.kpi_name,
                    sl.dimension,
                    sl.dimension_value,
                    sl.observed_value,
                    sl.threshold_value,
                    sl.breach_delta,
                    sl.signal_name,
                    sl.feature_name,
                    sl.feature_value,
                    sl.current_kpi_value,
                    sl.prev_kpi_value
                  FROM {SCHEMA}.signal_log sl
                  WHERE sl.signal_id = ANY(bk.signal_ids)
                    AND (
                      :all_timestamps
                      OR sl.detected_at = :batch_ts
                    )
                ) t
              ) AS signals_json,
              (
                SELECT COALESCE(json_agg(t), '[]'::json)
                FROM (
                  SELECT
                    ww.signal_id,
                    ww.kpi_name,
                    ww.dimension_name,
                    ww.dimension_value,
                    ww.signal_name,
                    ww.dep_kpi_name,
                    ww.current_value,
                    ww.prev_value,
                    ww.change_pct,
                    ww.period
                  FROM {SCHEMA}.why_results ww
                  WHERE ww.why_id = ANY(bk.why_ids)
                ) t
              ) AS why_results_json
            FROM bucket_keys bk
            ORDER BY bk.dimension_name, bk.dimension_value, bk.period_start, bk.period_end
            """
        )
        params: dict[str, Any] = {
            "dims_lower": dims,
            "all_timestamps": bool(all_timestamps),
            "batch_ts": batch_ts,
        }
        async with self._sf() as session:
            result = await session.execute(q, params)
            rows = result.mappings().all()

        out: list[dict[str, Any]] = []
        for r in rows:
            sj = r.get("signals_json")
            wj = r.get("why_results_json")
            if isinstance(sj, str):
                sj = json.loads(sj)
            if isinstance(wj, str):
                wj = json.loads(wj)
            if not isinstance(sj, list):
                sj = []
            if not isinstance(wj, list):
                wj = []
            out.append(
                {
                    "dimension_name": (r.get("dimension_name") or "").strip(),
                    "dimension_value": (r.get("dimension_value") or "").strip(),
                    "period_start": r.get("period_start"),
                    "period_end": r.get("period_end"),
                    "signals_json": sj,
                    "why_results_json": wj,
                }
            )
        return out

    @staticmethod
    def signal_from_signal_log_json_row(
        d: Mapping[str, Any],
        *,
        default_detected_at: datetime | None = None,
    ) -> Signal:
        """Build ``Signal`` from dimensional ``json_agg`` over the **subset** of ``signal_log`` columns.

        Full-row ``row_to_json(signal_log.*)`` is not required; missing columns get safe defaults.
        ``default_detected_at`` is used when ``detected_at`` is absent (subset JSON from SQL).
        """

        def _coerce_dt(val: Any) -> datetime:
            if val is None:
                return default_detected_at or datetime.now(timezone.utc)
            if isinstance(val, datetime):
                return val
            s = str(val).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return default_detected_at or datetime.now(timezone.utc)

        return Signal(
            signal_id=str(d["signal_id"]),
            kpi_name=str(d.get("kpi_name") or ""),
            dimension=str(d.get("dimension") or ""),
            dimension_value=str(d.get("dimension_value") or ""),
            signal_name=d.get("signal_name") or "",
            feature_name=d.get("feature_name") or "",
            feature_value=float(d["feature_value"]) if d.get("feature_value") is not None else 0.0,
            current_kpi_value=float(d["current_kpi_value"]) if d.get("current_kpi_value") is not None else None,
            prev_kpi_value=float(d["prev_kpi_value"]) if d.get("prev_kpi_value") is not None else None,
            observed_value=float(d["observed_value"]) if d.get("observed_value") is not None else 0.0,
            threshold_value=float(d["threshold_value"]) if d.get("threshold_value") is not None else 0.0,
            operator=str(d.get("operator") or ""),
            severity=str(d.get("severity") or ""),
            breach_delta=float(d["breach_delta"]) if d.get("breach_delta") is not None else 0.0,
            detected_at=_coerce_dt(d.get("detected_at")),
            why_computed=bool(d.get("why_computed")) if d.get("why_computed") is not None else False,
            job_id=(d.get("job_id") or "") or "",
            dax_kpi_query=d.get("dax_kpi_query"),
            dax_feature_query=d.get("dax_feature_query"),
        )

    async def get_signals_by_timestamp(self, ts: datetime) -> list[Signal]:
        async with self._sf() as session:
            stmt = select(signal_log_table).where(signal_log_table.c.detected_at == ts)
            result = await session.execute(stmt)
            db_rows = result.mappings().all()

        return self._signals_from_mappings(db_rows)

    async def get_signals_for_timestamp_and_kpi(self, ts: datetime, kpi_name: str) -> list[Signal]:
        """All ``signal_log`` rows for one ``detected_at`` batch and KPI (case-insensitive)."""
        k = (kpi_name or "").strip().lower()
        if not k:
            return []
        async with self._sf() as session:
            stmt = select(signal_log_table).where(
                signal_log_table.c.detected_at == ts,
                func.lower(func.trim(signal_log_table.c.kpi_name)) == k,
            )
            result = await session.execute(stmt)
            return self._signals_from_mappings(result.mappings().all())

    async def get_all_signals(self) -> list[Signal]:
        """Every row in ``signal_log`` (newest ``detected_at`` first)."""
        async with self._sf() as session:
            stmt = select(signal_log_table).order_by(desc(signal_log_table.c.detected_at))
            result = await session.execute(stmt)
            db_rows = result.mappings().all()

        return self._signals_from_mappings(db_rows)

    async def get_signals_latest_row_per_signal_ids(self, signal_ids: list[str]) -> list[Signal]:
        """For each ``signal_id``, the row with the latest ``detected_at`` (for narrative context)."""
        if not signal_ids:
            return []
        seen: list[str] = []
        for sid in signal_ids:
            if sid not in seen:
                seen.append(sid)
        ids = seen

        out: list[Signal] = []
        chunk_size = 1000
        async with self._sf() as session:
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i : i + chunk_size]
                latest = (
                    select(
                        signal_log_table.c.signal_id,
                        func.max(signal_log_table.c.detected_at).label("max_ts"),
                    )
                    .where(signal_log_table.c.signal_id.in_(chunk))
                    .group_by(signal_log_table.c.signal_id)
                    .subquery()
                )
                stmt = select(signal_log_table).join(
                    latest,
                    and_(
                        signal_log_table.c.signal_id == latest.c.signal_id,
                        signal_log_table.c.detected_at == latest.c.max_ts,
                    ),
                )
                result = await session.execute(stmt)
                out.extend(self._signals_from_mappings(result.mappings().all()))

        return out

    def _signals_from_mappings(self, db_rows: list) -> list[Signal]:
        return [
            Signal(
                signal_id=str(r["signal_id"]),
                kpi_name=r["kpi_name"],
                dimension=r["dimension"],
                dimension_value=r["dimension_value"],
                signal_name=r.get("signal_name") or "",
                feature_name=r.get("feature_name") or "",
                feature_value=float(r["feature_value"]) if r.get("feature_value") is not None else 0.0,
                current_kpi_value=float(r["current_kpi_value"]) if r.get("current_kpi_value") is not None else None,
                prev_kpi_value=float(r["prev_kpi_value"]) if r.get("prev_kpi_value") is not None else None,
                observed_value=float(r["observed_value"]),
                threshold_value=float(r["threshold_value"]),
                operator=r["operator"],
                severity=r["severity"],
                breach_delta=float(r["breach_delta"]),
                detected_at=r["detected_at"],
                why_computed=r["why_computed"],
                job_id=r["job_id"] or "",
            )
            for r in db_rows
        ]

    async def get_signals_latest_per_kpi_dimension(self) -> list[Signal]:
        """Rows whose ``detected_at`` is the latest for each ``(kpi_name, dimension)`` pair.

        Use this when jobs stamp different ``detected_at`` values so alpha (or other)
        per-slice logic still sees every KPI×dimension slice from its newest batch.
        """
        latest = (
            select(
                signal_log_table.c.kpi_name,
                signal_log_table.c.dimension,
                func.max(signal_log_table.c.detected_at).label("max_ts"),
            )
            .group_by(signal_log_table.c.kpi_name, signal_log_table.c.dimension)
            .subquery()
        )
        async with self._sf() as session:
            stmt = select(signal_log_table).join(
                latest,
                and_(
                    signal_log_table.c.kpi_name == latest.c.kpi_name,
                    signal_log_table.c.dimension == latest.c.dimension,
                    signal_log_table.c.detected_at == latest.c.max_ts,
                ),
            )
            result = await session.execute(stmt)
            db_rows = result.mappings().all()

        return self._signals_from_mappings(db_rows)

    async def get_whys_for_signals(self, signal_ids: list[str]) -> list[WhyRow]:
        if not signal_ids:
            return []
        db_rows: list[Any] = []
        async with self._sf() as session:
            for i in range(0, len(signal_ids), _PG_IN_CLAUSE_BATCH):
                chunk = signal_ids[i : i + _PG_IN_CLAUSE_BATCH]
                stmt = select(why_results_table).where(
                    why_results_table.c.signal_id.in_(chunk)
                )
                why_result = await session.execute(stmt)
                db_rows.extend(why_result.mappings().all())

        return [
            WhyRow(
                why_id=str(r["why_id"]),
                signal_id=str(r["signal_id"]),
                run_timestamp=r["run_timestamp"],
                kpi_name=r["kpi_name"],
                dimension_name=r["dimension_name"],
                dimension_value=r["dimension_value"],
                signal_name=r.get("signal_name") or "",
                dep_kpi_name=r.get("dep_kpi_name"),
                dep_kpi_label=r.get("dep_kpi_label"),
                rationale=r.get("rationale") or "",
                current_value=float(r["current_value"]) if r.get("current_value") is not None else None,
                prev_value=float(r["prev_value"]) if r.get("prev_value") is not None else None,
                change_pct=float(r["change_pct"]) if r.get("change_pct") is not None else None,
                period=r.get("period") or "",
                period_start=r.get("period_start"),
                period_end=r.get("period_end"),
                created_at=r.get("created_at"),
            )
            for r in db_rows
        ]

    async def write_main_insights(self, insights: list[MainInsight]) -> int:
        if not insights:
            return 0
        mi_table = self._main_insights_table()

        def _row(ins: MainInsight) -> dict[str, Any]:
            iid = ins.insight_id
            rid = PyUUID(str(iid)) if iid else uuid4()
            out: dict[str, Any] = {
                "insight_id": rid,
                "run_timestamp": ins.run_timestamp,
                "signal_ids": ins.signal_ids,
                "kpi_family": ins.kpi_family,
                "title": ins.title,
                "kpi": ins.kpi,
                "dimension_name": ins.dimension_name,
                "dimension_value": ins.dimension_value,
                "insight": ins.insight,
                "why": ins.why,
                "period": ins.period,
                "period_start": ins.period_start,
                "period_end": ins.period_end,
                "insight_summary": ins.insight_summary,
                "why_insight_summary": ins.why_insight_summary,
                "severity": ins.severity,
                "tags": ins.tags,
                "impact_insight": ins.impact_insight,
                "park": ins.park,
                "hook": None,
                "like": ins.like,
                "dislike": ins.dislike,
                "remarks": ins.remarks,
                "recommended_actions": ins.recommended_actions,
            }
            if ins.created_at is not None:
                out["created_at"] = ins.created_at
            return out

        rows = [_row(ins) for ins in insights]
        async with self._sf() as session:
            for i in range(0, len(rows), _MAIN_INSIGHTS_INSERT_BATCH):
                batch = rows[i : i + _MAIN_INSIGHTS_INSERT_BATCH]
                await session.execute(insert(mi_table), batch)
            await session.commit()
        logger.debug(
            "write_main_insights | inserted=%s in %s batch(es) of up to %s",
            len(rows),
            (len(rows) + _MAIN_INSIGHTS_INSERT_BATCH - 1) // _MAIN_INSIGHTS_INSERT_BATCH,
            _MAIN_INSIGHTS_INSERT_BATCH,
        )
        return len(insights)

    async def list_signal_log_rows(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        """All columns from ``insights.signal_log`` (newest first), paginated."""
        cap = max(1, min(limit, 5000))
        off = max(0, offset)
        async with self._sf() as session:
            stmt = (
                select(signal_log_table)
                .order_by(desc(signal_log_table.c.detected_at))
                .limit(cap)
                .offset(off)
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
        return [_mapping_to_jsonable(r) for r in rows]

    async def list_why_result_rows(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        """All columns from ``insights.why_results`` (newest first), paginated."""
        cap = max(1, min(limit, 5000))
        off = max(0, offset)
        async with self._sf() as session:
            stmt = (
                select(why_results_table)
                .order_by(desc(why_results_table.c.created_at))
                .limit(cap)
                .offset(off)
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
        return [_mapping_to_jsonable(r) for r in rows]

    async def list_main_insight_rows(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        pascal_case: bool = True,
        run_timestamp: datetime | None = None,
        kpi_family: str | None = None,
    ) -> list[dict[str, Any]]:
        """Rows from ``insights.main_insights`` (newest ``created_at`` first).

        When ``pascal_case`` is True, keys match ``vw_main_insights`` / frontend grid
        (InsightID, RunTimestamp, …, Hook). Otherwise raw snake_case column names.
        """
        cap = max(1, min(limit, 5000))
        off = max(0, offset)
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = select(mi_table)
            if run_timestamp is not None:
                stmt = stmt.where(mi_table.c.run_timestamp == run_timestamp)
            if kpi_family:
                stmt = stmt.where(mi_table.c.kpi_family == kpi_family)
            stmt = (
                stmt.order_by(desc(mi_table.c.created_at))
                .limit(cap)
                .offset(off)
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
        if pascal_case:
            return [_main_insight_row_to_pascal(r) for r in rows]
        return [_mapping_to_jsonable(r) for r in rows]

    async def get_main_insight_by_id(
        self, insight_id: PyUUID, *, pascal_case: bool = True
    ) -> dict[str, Any] | None:
        """Single row from ``insights.main_insights`` by primary key."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = select(mi_table).where(mi_table.c.insight_id == insight_id)
            result = await session.execute(stmt)
            row = result.mappings().first()
        if row is None:
            return None
        if pascal_case:
            return _main_insight_row_to_pascal(row)
        return _mapping_to_jsonable(row)

    async def get_finalcrm_by_group(self, group_name: str) -> dict[str, Any] | None:
        """Precomputed executive summary row for a KPI-card group, or None."""
        key = (group_name or "").strip().lower()
        if not key:
            return None
        async with self._sf() as session:
            stmt = select(finalcrm_table).where(
                func.lower(finalcrm_table.c.group_name) == key
            )
            row = (await session.execute(stmt)).mappings().first()
        return _mapping_to_jsonable(row) if row is not None else None

    async def update_main_insight_feedback(
        self, insight_id: PyUUID, updates: dict[str, Any]
    ) -> int:
        """Update ``like``, ``dislike``, ``remarks``, and/or ``park`` on one row. Returns rows affected (0 or 1)."""
        allowed = frozenset({"like", "dislike", "remarks", "park"})
        payload = {k: v for k, v in updates.items() if k in allowed}
        if not payload:
            return 0
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(**payload)
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def get_latest_main_insight_run_timestamp(self) -> datetime | None:
        """``run_timestamp`` of the most recently created main insight row."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                select(mi_table.c.run_timestamp)
                .order_by(desc(mi_table.c.created_at))
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    async def list_main_insights_for_recommended_actions(
        self,
        *,
        run_timestamp: datetime | None = None,
        insight_id: PyUUID | None = None,
        skip_existing: bool = False,
    ) -> list[dict[str, Any]]:
        """Rows with text fields needed to generate ``recommended_actions``."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = select(
                mi_table.c.insight_id,
                mi_table.c.run_timestamp,
                mi_table.c.title,
                mi_table.c.kpi,
                mi_table.c.dimension_name,
                mi_table.c.dimension_value,
                mi_table.c.insight,
                mi_table.c.why,
                mi_table.c.insight_summary,
                mi_table.c.why_insight_summary,
                mi_table.c.impact_insight,
                mi_table.c.recommended_actions,
            )
            if insight_id is not None:
                stmt = stmt.where(mi_table.c.insight_id == insight_id)
            elif run_timestamp is not None:
                stmt = stmt.where(mi_table.c.run_timestamp == run_timestamp)
            stmt = stmt.order_by(desc(mi_table.c.created_at))
            result = await session.execute(stmt)
            rows = [dict(r) for r in result.mappings().all()]
        if skip_existing:
            rows = [
                r
                for r in rows
                if not (r.get("recommended_actions") or "").strip()
            ]
        return rows

    async def update_main_insight_recommended_actions(
        self, insight_id: PyUUID, recommended_actions: str
    ) -> int:
        """Persist LLM-generated comma-separated actions. Returns rows affected (0 or 1)."""
        text = (recommended_actions or "")[:16000]
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(recommended_actions=text)
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def list_main_insights_for_why_summarization(
        self,
        *,
        run_timestamp: datetime | None = None,
        insight_id: PyUUID | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Rows needed to summarize ``why`` using an LLM."""
        cap = max(1, min(int(limit), 10_000))
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = select(
                mi_table.c.insight_id,
                mi_table.c.run_timestamp,
                mi_table.c.title,
                mi_table.c.kpi,
                mi_table.c.dimension_name,
                mi_table.c.dimension_value,
                mi_table.c.why,
            )
            if insight_id is not None:
                stmt = stmt.where(mi_table.c.insight_id == insight_id)
            elif run_timestamp is not None:
                stmt = stmt.where(mi_table.c.run_timestamp == run_timestamp)
            stmt = stmt.order_by(desc(mi_table.c.created_at)).limit(cap)
            result = await session.execute(stmt)
            return [dict(r) for r in result.mappings().all()]

    async def update_main_insight_why(self, insight_id: PyUUID, why: str) -> int:
        """Persist summarized ``why`` text. Returns rows affected (0 or 1)."""
        text_val = (why or "")[:48000]
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(why=text_val)
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def update_main_insight_only(
        self, insight_id: PyUUID, insight: str
    ) -> int:
        """Replace the ``insight`` column entirely. Does NOT touch insight_summary. Returns rows affected."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(insight=(insight or "")[:48000])
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def update_main_insight_what(
        self, insight_id: PyUUID, insight: str, insight_summary: str
    ) -> int:
        """Persist refined ``insight`` (problem_statement) and ``insight_summary``. Returns rows affected."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(
                    insight=(insight or "")[:48000],
                    insight_summary=(insight_summary or "")[:16000],
                )
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def update_main_insight_summary_only(
        self, insight_id: PyUUID, insight_summary: str
    ) -> int:
        """Persist updated ``insight_summary`` only. Returns rows affected."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(insight_summary=(insight_summary or "")[:16000])
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def update_main_insight_why_and_summary(
        self, insight_id: PyUUID, why: str, why_insight_summary: str
    ) -> int:
        """Persist refined ``why`` and ``why_insight_summary``. Returns rows affected."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(
                    why=(why or "")[:48000],
                    why_insight_summary=(why_insight_summary or "")[:16000],
                )
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def list_main_insights_for_refinement(
        self,
        *,
        insight_id: PyUUID | None = None,
        run_timestamp: datetime | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Full row data needed to refine insight/why via LLM."""
        cap = max(1, min(int(limit), 10_000))
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = select(
                mi_table.c.insight_id,
                mi_table.c.run_timestamp,
                mi_table.c.title,
                mi_table.c.kpi,
                mi_table.c.dimension_name,
                mi_table.c.dimension_value,
                mi_table.c.insight,
                mi_table.c.why,
                mi_table.c.insight_summary,
                mi_table.c.why_insight_summary,
                mi_table.c.impact_insight,
                mi_table.c.severity,
                mi_table.c.tags,
            )
            if insight_id is not None:
                stmt = stmt.where(mi_table.c.insight_id == insight_id)
            elif run_timestamp is not None:
                stmt = stmt.where(mi_table.c.run_timestamp == run_timestamp)
            stmt = stmt.order_by(desc(mi_table.c.created_at)).limit(cap)
            result = await session.execute(stmt)
            return [dict(r) for r in result.mappings().all()]

    async def list_main_insights_for_markup_reformat(
        self,
        *,
        insight_id: Optional[PyUUID] = None,
        run_timestamp: Optional[datetime] = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Rows for formatting-only passes (title + narrative columns + recommended_actions)."""
        cap = max(1, min(int(limit), 10_000))
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = select(
                mi_table.c.insight_id,
                mi_table.c.dimension_name,
                mi_table.c.dimension_value,
                mi_table.c.title,
                mi_table.c.insight,
                mi_table.c.why,
                mi_table.c.insight_summary,
                mi_table.c.why_insight_summary,
                mi_table.c.impact_insight,
                mi_table.c.recommended_actions,
            )
            if insight_id is not None:
                stmt = stmt.where(mi_table.c.insight_id == insight_id)
            elif run_timestamp is not None:
                stmt = stmt.where(mi_table.c.run_timestamp == run_timestamp)
            stmt = stmt.order_by(desc(mi_table.c.created_at)).limit(cap)
            result = await session.execute(stmt)
            return [dict(r) for r in result.mappings().all()]

    async def update_main_insight_markup_columns(
        self,
        insight_id: PyUUID,
        *,
        title: str | None,
        insight: str,
        why: str | None,
        insight_summary: str | None,
        why_insight_summary: str | None,
        impact_insight: str | None,
        recommended_actions: str | None,
    ) -> int:
        """Persist reformatted markup fields (no LLM). Returns rows affected (0 or 1)."""
        mi_table = self._main_insights_table()
        async with self._sf() as session:
            stmt = (
                update(mi_table)
                .where(mi_table.c.insight_id == insight_id)
                .values(
                    title=title,
                    insight=insight,
                    why=why,
                    insight_summary=insight_summary,
                    why_insight_summary=why_insight_summary,
                    impact_insight=impact_insight,
                    recommended_actions=recommended_actions,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
        return int(result.rowcount or 0)

    async def list_correlation_priors(
        self,
        target_kpis: list[str],
        *,
        max_p_value: float = 0.10,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch correlation priors for target KPI names (case-insensitive)."""
        keys = sorted({str(x).strip().lower() for x in target_kpis if str(x).strip()})
        if not keys:
            return []
        async with self._sf() as session:
            stmt = select(correlation_priors_table).where(
                func.lower(correlation_priors_table.c.target_kpi).in_(keys)
            )
            if active_only:
                stmt = stmt.where(correlation_priors_table.c.is_active.is_(True))
            try:
                mp = float(max_p_value)
            except Exception:
                mp = 0.10
            stmt = stmt.where(
                (correlation_priors_table.c.p_value.is_(None))
                | (correlation_priors_table.c.p_value <= mp)
            )
            stmt = stmt.order_by(
                correlation_priors_table.c.target_kpi.asc(),
                correlation_priors_table.c.p_value.asc().nullslast(),
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
        return [_mapping_to_jsonable(r) for r in rows]

    async def list_anomaly_timeline(
        self,
        target_kpis: list[str],
        *,
        weeks_back: int = 16,
        active_only: bool = True,
        anomaly_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch recent anomaly timeline rows for target KPI names (case-insensitive)."""
        keys = sorted({str(x).strip().lower() for x in target_kpis if str(x).strip()})
        if not keys:
            return []
        cutoff = (datetime.now(timezone.utc).date() - timedelta(weeks=max(1, int(weeks_back))))
        async with self._sf() as session:
            stmt = select(anomaly_timeline_table).where(
                func.lower(anomaly_timeline_table.c.kpi_name).in_(keys)
            )
            if active_only:
                stmt = stmt.where(anomaly_timeline_table.c.is_active.is_(True))
            stmt = stmt.where(anomaly_timeline_table.c.week_start_date >= cutoff)
            if anomaly_only:
                stmt = stmt.where(anomaly_timeline_table.c.anomaly_flag == -1)
            stmt = stmt.order_by(
                anomaly_timeline_table.c.kpi_name.asc(),
                anomaly_timeline_table.c.week_start_date.asc(),
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
        return [_mapping_to_jsonable(r) for r in rows]
