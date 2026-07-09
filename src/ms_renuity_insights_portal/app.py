"""FastAPI application factory for the Power BI DAX analytics engine."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.pbi import pbi_router

logger = logging.getLogger(__name__)


def _configure_quiet_loggers() -> None:
    """Suppress SQL echo and chatty HTTP/token SDK logs unless opt-in."""
    if os.environ.get("INSIGHTS_LOG_SQL", "").strip().lower() in ("1", "true", "yes"):
        sql_level = logging.INFO
    else:
        sql_level = logging.WARNING
    for name in ("sqlalchemy.engine", "sqlalchemy.engine.Engine", "sqlalchemy.pool"):
        logging.getLogger(name).setLevel(sql_level)

    if os.environ.get("INSIGHTS_VERBOSE_HTTP", "").strip().lower() not in ("1", "true", "yes"):
        for name in (
            "azure.core.pipeline.policies.http_logging_policy",
            "azure.identity",
            "azure.identity.aio",
            "httpx",
            "httpcore",
            "httpcore.http11",
            "httpcore.connection",
            "urllib3",
        ):
            logging.getLogger(name).setLevel(logging.WARNING)

    cfg_log = logging.getLogger("ms_renuity_insights_portal.config.config_loader")
    if os.environ.get("INSIGHTS_VERBOSE_CONFIG", "").strip().lower() in ("1", "true", "yes"):
        cfg_log.setLevel(logging.DEBUG)
    else:
        cfg_log.setLevel(logging.WARNING)

    feat_log = logging.getLogger("ms_renuity_insights_portal.engine.feature_generator")
    if os.environ.get("INSIGHTS_VERBOSE_DAX", "").strip().lower() in ("1", "true", "yes"):
        feat_log.setLevel(logging.DEBUG)
    else:
        feat_log.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_quiet_loggers()
    try:
        from .api.dependencies import get_result_store

        store = get_result_store()
        if store is not None:
            await store.ensure_tables()
            logger.info("Result-store tables (kpi_results, signal_log, why_results) ensured.")
    except Exception:
        logger.warning("Could not auto-create result-store tables at startup.", exc_info=True)

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="MS Insights CRM — PBI DAX Engine",
        description=(
            "CRM-dedicated fork: KPI analytics, signal detection, and WHY analysis "
            "powered by Power BI DAX queries."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", tags=["Root"], include_in_schema=True)
    def root() -> dict:
        """Service entry; Swagger UI stays at /docs (not under /pbi)."""
        return {
            "service": app.title,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "pbi_api": "/pbi",
            "health": "/pbi/health",
            "signal_log": "/pbi/store/signal-log",
            "why_results": "/pbi/store/why-results",
            "main_insights": "/pbi/insights/main-insights",
            "main_insights_generate_standard_post": "/pbi/insights/main-insights/generate-standard",
            "main_insights_generate_dimensional_post": "/pbi/insights/main-insights/generate-dimensional",
            "main_insights_generate_kpi_post": "/pbi/insights/main-insights/generate-kpi",
            "main_insights_recommended_actions_post": "/pbi/insights/main-insights/recommended-actions",
            "main_insights_summarize_why_sonnet_post": "/pbi/insights/main-insights/summarize-why-sonnet",
            "main_insights_reformat_why_structure_sonnet_post": "/pbi/insights/main-insights/reformat-why-structure-sonnet",
            "main_insights_reformat_markup_post": "/pbi/insights/main-insights/reformat-markup",
            "main_insights_feedback_put": "/pbi/insights/main-insights/{insight_id}",
            "main_insights_store": "/pbi/store/main-insights",
            "portal_derived_kpis": "/pbi/portal/derived-kpis",
            "portal_derived_kpis_breakdown": "/pbi/portal/derived-kpis/breakdown",
            "portal_executive_summary": "/pbi/portal/main-insights/executive-summary",
            "portal_insight_breakdown": "/pbi/portal/main-insights/{insight_id}/breakdown",
            "trigger_signals": "/pbi/trigger/signals",
            "trigger_whys": "/pbi/trigger/whys",
            "trigger_whys_batch": "/pbi/trigger/whys/batch",
            "trigger_whys_by_kpis": "/pbi/trigger/whys/by-kpis",
            "trigger_full_insights_pipeline": "/pbi/trigger/full-insights-pipeline",
            "trigger_full_insights_pipeline_stream": "/pbi/trigger/full-insights-pipeline/stream",
        }

    app.include_router(pbi_router, prefix="/pbi")

    _configure_quiet_loggers()
    return app


app = create_app()
logger.info(
    "OpenAPI service title=%r — routes under /pbi; docs at /docs",
    app.title,
)
