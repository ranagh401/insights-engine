"""Health-check endpoint for the PBI DAX engine."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..dependencies import get_dax_settings, get_result_store
from ...settings import Settings
from ...store.result_store import ResultStore

router = APIRouter()


@router.get("/health", response_model=dict[str, Any])
async def health(
    settings: Settings = Depends(get_dax_settings),
    store: ResultStore = Depends(get_result_store),
):
    db_ok = True
    pbi_configured = settings is not None

    if pbi_configured:
        try:
            await store.get_unprocessed_signals(limit=1, order_detected_at="asc")
        except Exception:
            db_ok = False

    return {
        "status": "ok" if (db_ok and pbi_configured) else "degraded",
        "dataset_id": settings.PBI_DATASET_ID if pbi_configured else None,
        "db_connected": db_ok,
        "pbi_configured": pbi_configured,
        "version": "1.0.0",
    }
