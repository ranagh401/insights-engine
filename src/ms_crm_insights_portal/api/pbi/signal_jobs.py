"""CRUD endpoints for Signal Job configuration (config_signal_jobs)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.session import get_db
from ...models.config_signal_job import ConfigSignalJob

router = APIRouter()


class SignalJobCreate(BaseModel):
    job_name: Optional[str] = ""
    pbi_measure_name: Optional[str] = None
    kpi_name: str
    dimensions: list[str]
    features: list[str]
    signals: list[str]
    filters: Optional[dict] = None
    feature_params: Optional[dict] = None
    is_active: bool = True


class SignalJobUpdate(BaseModel):
    job_name: Optional[str] = None
    pbi_measure_name: Optional[str] = None
    kpi_name: Optional[str] = None
    dimensions: Optional[list[str]] = None
    features: Optional[list[str]] = None
    signals: Optional[list[str]] = None
    filters: Optional[dict] = None
    feature_params: Optional[dict] = None
    is_active: Optional[bool] = None


@router.get("", response_model=list[dict[str, Any]])
async def list_signal_jobs(
    active_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ConfigSignalJob).order_by(ConfigSignalJob.job_id)
    if active_only:
        stmt = stmt.where(ConfigSignalJob.is_active == True)  # noqa: E712
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_to_dict(r) for r in rows]


@router.get("/{job_id}", response_model=dict[str, Any])
async def get_signal_job(job_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigSignalJob, job_id)
    if not row:
        raise HTTPException(404, f"Signal job {job_id} not found")
    return _to_dict(row)


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_signal_job(body: SignalJobCreate, db: AsyncSession = Depends(get_db)):
    row = ConfigSignalJob(**body.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


@router.put("/{job_id}", response_model=dict[str, Any])
async def update_signal_job(job_id: int, body: SignalJobUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigSignalJob, job_id)
    if not row:
        raise HTTPException(404, f"Signal job {job_id} not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


@router.delete("/{job_id}", status_code=204)
async def delete_signal_job(job_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigSignalJob, job_id)
    if not row:
        raise HTTPException(404, f"Signal job {job_id} not found")
    await db.delete(row)


@router.patch("/{job_id}/toggle", response_model=dict[str, Any])
async def toggle_signal_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Toggle is_active flag on a signal job."""
    row = await db.get(ConfigSignalJob, job_id)
    if not row:
        raise HTTPException(404, f"Signal job {job_id} not found")
    row.is_active = not row.is_active
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


def _to_dict(row: ConfigSignalJob) -> dict[str, Any]:
    return {
        "job_id": row.job_id,
        "job_name": row.job_name,
        "pbi_measure_name": row.pbi_measure_name,
        "kpi_name": row.kpi_name,
        "dimensions": row.dimensions,
        "features": row.features,
        "signals": row.signals,
        "filters": row.filters,
        "feature_params": row.feature_params,
        "is_active": row.is_active,
        "created_at": str(row.created_at) if row.created_at else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }
