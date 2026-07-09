"""PBI (Power BI DAX) API router — aggregates all /pbi sub-routers."""

from fastapi import APIRouter

from .kpis import router as kpis_router
from .features import router as features_router
from .signals import router as signals_router
from .signal_jobs import router as signal_jobs_router
from .trigger import router as trigger_router
from .health import router as health_router
from .insights import router as insights_router
from .store_reads import router as store_reads_router
from .data import router as data_router
from .portal import router as portal_router

pbi_router = APIRouter()

pbi_router.include_router(kpis_router, prefix="/kpis", tags=["PBI - KPIs"])
pbi_router.include_router(features_router, prefix="/features", tags=["PBI - Features"])
pbi_router.include_router(signals_router, prefix="/signals", tags=["PBI - Signals"])
pbi_router.include_router(signal_jobs_router, prefix="/signal-jobs", tags=["PBI - Signal Jobs"])
pbi_router.include_router(trigger_router, prefix="/trigger", tags=["PBI - Trigger"])
pbi_router.include_router(insights_router, prefix="/insights", tags=["PBI - Insights"])
pbi_router.include_router(store_reads_router, prefix="/store", tags=["PBI - Store"])
pbi_router.include_router(data_router, prefix="/data", tags=["PBI - Data"])
pbi_router.include_router(portal_router, prefix="/portal", tags=["PBI - Portal"])
pbi_router.include_router(health_router, tags=["PBI - Health"])
