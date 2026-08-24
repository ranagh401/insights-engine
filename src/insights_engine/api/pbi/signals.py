"""CRUD endpoints for Signal definitions (config_signals)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.session import get_db
from ...models.config_signal import ConfigSignal

router = APIRouter()


class SignalCreate(BaseModel):
    signal_name: str
    feature_name: str
    operator: str
    threshold: float
    threshold2: Optional[float] = None
    severity: str
    message_template: str
    top_n: Optional[int] = None
    format: str = "percentage"
    dimensions: Optional[dict] = None


class SignalUpdate(BaseModel):
    feature_name: Optional[str] = None
    operator: Optional[str] = None
    threshold: Optional[float] = None
    threshold2: Optional[float] = None
    severity: Optional[str] = None
    message_template: Optional[str] = None
    top_n: Optional[int] = None
    format: Optional[str] = None
    dimensions: Optional[dict] = None


@router.get("", response_model=list[dict[str, Any]])
async def list_signals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ConfigSignal).order_by(ConfigSignal.signal_name))
    rows = result.scalars().all()
    return [_to_dict(r) for r in rows]


@router.get("/{signal_name}", response_model=dict[str, Any])
async def get_signal(signal_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigSignal, signal_name)
    if not row:
        raise HTTPException(404, f"Signal '{signal_name}' not found")
    return _to_dict(row)


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_signal(body: SignalCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.get(ConfigSignal, body.signal_name)
    if existing:
        raise HTTPException(409, f"Signal '{body.signal_name}' already exists")
    row = ConfigSignal(**body.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


@router.put("/{signal_name}", response_model=dict[str, Any])
async def update_signal(signal_name: str, body: SignalUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigSignal, signal_name)
    if not row:
        raise HTTPException(404, f"Signal '{signal_name}' not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


@router.delete("/{signal_name}", status_code=204)
async def delete_signal(signal_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigSignal, signal_name)
    if not row:
        raise HTTPException(404, f"Signal '{signal_name}' not found")
    await db.delete(row)


def _to_dict(row: ConfigSignal) -> dict[str, Any]:
    return {
        "signal_name": row.signal_name,
        "feature_name": row.feature_name,
        "operator": row.operator,
        "threshold": row.threshold,
        "threshold2": row.threshold2,
        "severity": row.severity,
        "message_template": row.message_template,
        "top_n": row.top_n,
        "format": row.format,
        "dimensions": row.dimensions,
        "created_at": str(row.created_at) if row.created_at else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }
