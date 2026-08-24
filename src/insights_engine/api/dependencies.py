"""API dependencies — Dependency Injection for FastAPI endpoints.

Provides both the existing insights service dependencies and the new
DAX engine component singletons.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from ..config.config import get_settings

logger = logging.getLogger(__name__)


async def get_jwt_manager():
    """Provide a JWTManager instance configured with insights service settings."""
    settings = get_settings()
    return await settings.get_jwt_manager()


def get_insights_settings():
    """Return the InsightsServiceConfig singleton."""
    return get_settings()


@lru_cache()
def get_dax_settings():
    """Return the DAX engine Settings singleton."""
    from ..settings import Settings

    try:
        s = Settings()
        logger.info(
            "DAX settings loaded: dataset=%s, date_table=%s[%s]",
            s.PBI_DATASET_ID, s.DATE_TABLE_NAME, s.DATE_COLUMN_NAME,
        )
        return s
    except Exception:
        logger.error("DAX Settings() failed to initialize!", exc_info=True)
        return None


@lru_cache()
def _build_pbi_client():
    settings = get_dax_settings()
    if settings is None:
        return None
    from ..powerbi.api_client import PBIClient

    return PBIClient(
        tenant_id=settings.AZURE_TENANT_ID,
        client_id=settings.AZURE_CLIENT_ID,
        client_secret=settings.AZURE_CLIENT_SECRET,
        workspace_id=settings.PBI_WORKSPACE_ID,
        dataset_id=settings.PBI_DATASET_ID,
        max_concurrent=settings.PBI_MAX_CONCURRENT_QUERIES,
        max_retries=settings.PBI_RETRY_MAX_ATTEMPTS,
        read_timeout_seconds=settings.PBI_DAX_READ_TIMEOUT_SECONDS,
    )


@lru_cache()
def _build_session_factory():
    from ..db.session import async_session_factory

    return async_session_factory


@lru_cache()
def _build_engine_obj():
    from ..db.session import engine as db_engine

    return db_engine


def get_config_loader():
    from ..config.config_loader import ConfigLoader

    return ConfigLoader(_build_session_factory())


def get_result_store():
    from ..store.result_store import ResultStore, normalize_main_insights_table_name

    settings = get_dax_settings()
    table_name = (
        normalize_main_insights_table_name(settings.MAIN_INSIGHTS_TABLE)
        if settings is not None
        else "maininsightsportal"
    )
    return ResultStore(
        _build_engine_obj(),
        _build_session_factory(),
        main_insights_table_name=table_name,
    )


def get_kpi_engine():
    from ..engine.kpi_engine import KPIEngine

    return KPIEngine(
        config_loader=get_config_loader(),
        pbi_client=_build_pbi_client(),
        result_store=get_result_store(),
        settings=get_dax_settings(),
    )


def get_why_analyzer():
    from ..engine.why_analyzer import WhyAnalyzer

    return WhyAnalyzer(
        kpi_engine=get_kpi_engine(),
        config_loader=get_config_loader(),
        result_store=get_result_store(),
        feature_generator=get_feature_generator(),
    )


def get_feature_generator():
    from ..engine.feature_generator import FeatureGenerator

    return FeatureGenerator(
        config_loader=get_config_loader(),
        pbi_client=_build_pbi_client(),
        settings=get_dax_settings(),
    )


def get_pbi_client():
    """Return singleton Power BI API client (or None if settings invalid)."""
    return _build_pbi_client()


def create_pipeline_feature_generator(*, run_label: str | None = None):
    """Fresh ``FeatureGenerator`` sharing the singleton PBI client.

    Use one instance per concurrent signal job so per-job DAX metadata on the
    generator does not race; DAX concurrency stays bounded by ``PBIClient``'s
    semaphore.

    ``run_label`` is included on INFO/DEBUG lines so logs show which signal job
    is executing DAX (e.g. ``job 15/217 id=132 kpi=...``).
    """
    from ..engine.feature_generator import FeatureGenerator

    pbi = _build_pbi_client()
    settings = get_dax_settings()
    if pbi is None or settings is None:
        raise RuntimeError("Power BI client or DAX settings are not configured.")
    return FeatureGenerator(
        config_loader=get_config_loader(),
        pbi_client=pbi,
        settings=settings,
        run_label=run_label,
    )
