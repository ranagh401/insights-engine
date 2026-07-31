"""CRUD endpoints for KPI configuration (insights.configkpisrenuitycrm)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.session import get_db
from ...models.config_kpi import ConfigKPI

router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class KPICreate(BaseModel):
    kpi_name: str
    measure: str
    label: str
    format: str
    description: Optional[str] = ""
    from_table: str = ""
    is_main_kpi: bool = False
    pbi_measure_name: Optional[str] = None


class KPIUpdate(BaseModel):
    measure: Optional[str] = None
    label: Optional[str] = None
    format: Optional[str] = None
    description: Optional[str] = None
    from_table: Optional[str] = None
    is_main_kpi: Optional[bool] = None
    pbi_measure_name: Optional[str] = None


# ── Base KPIs ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[dict[str, Any]])
async def list_kpis(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ConfigKPI).order_by(ConfigKPI.kpi_name))
    rows = result.scalars().all()
    return [_kpi_to_dict(r) for r in rows]


# NOTE: declared before "/{kpi_name}" so the literal path wins route matching.
@router.get("/descriptions", response_model=list[dict[str, Any]])
async def list_kpi_descriptions(db: AsyncSession = Depends(get_db)):
    """Description of each KPI in insights.configkpisrenuitycrm."""
    result = await db.execute(
        select(ConfigKPI.kpi_name, ConfigKPI.label, ConfigKPI.description).order_by(
            ConfigKPI.kpi_name
        )
    )
    return [
        {"kpi_name": kpi_name, "label": label, "description": description or ""}
        for kpi_name, label, description in result.all()
    ]


@router.get("/{kpi_name}", response_model=dict[str, Any])
async def get_kpi(kpi_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigKPI, kpi_name)
    if not row:
        raise HTTPException(404, f"KPI '{kpi_name}' not found")
    return _kpi_to_dict(row)


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_kpi(body: KPICreate, db: AsyncSession = Depends(get_db)):
    existing = await db.get(ConfigKPI, body.kpi_name)
    if existing:
        raise HTTPException(409, f"KPI '{body.kpi_name}' already exists")
    row = ConfigKPI(**body.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _kpi_to_dict(row)


@router.put("/{kpi_name}", response_model=dict[str, Any])
async def update_kpi(kpi_name: str, body: KPIUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigKPI, kpi_name)
    if not row:
        raise HTTPException(404, f"KPI '{kpi_name}' not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    await db.refresh(row)
    return _kpi_to_dict(row)


@router.delete("/{kpi_name}", status_code=204)
async def delete_kpi(kpi_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigKPI, kpi_name)
    if not row:
        raise HTTPException(404, f"KPI '{kpi_name}' not found")
    await db.delete(row)


# ── Portal KPI aliases (same table as base KPIs in CRM) ───────────────────────

@router.get("/derived/all", response_model=list[dict[str, Any]])
async def list_derived_kpis(db: AsyncSession = Depends(get_db)):
    """List all KPIs from configkpisrenuitycrm (CRM: no separate derived-KPI table)."""
    result = await db.execute(select(ConfigKPI).order_by(ConfigKPI.kpi_name))
    rows = result.scalars().all()
    return [_kpi_to_dict(r) for r in rows]


@router.get("/derived/{kpi_name}", response_model=dict[str, Any])
async def get_derived_kpi(kpi_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigKPI, kpi_name)
    if not row:
        raise HTTPException(404, f"KPI '{kpi_name}' not found in configkpisrenuitycrm")
    return _kpi_to_dict(row)


@router.post("/derived", response_model=dict[str, Any], status_code=201)
async def create_derived_kpi(body: KPICreate, db: AsyncSession = Depends(get_db)):
    """Create a KPI row in configkpisrenuitycrm."""
    existing = await db.get(ConfigKPI, body.kpi_name)
    if existing:
        raise HTTPException(409, f"KPI '{body.kpi_name}' already exists")
    row = ConfigKPI(**body.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _kpi_to_dict(row)


@router.put("/derived/{kpi_name}", response_model=dict[str, Any])
async def update_derived_kpi(kpi_name: str, body: KPIUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigKPI, kpi_name)
    if not row:
        raise HTTPException(404, f"KPI '{kpi_name}' not found in configkpisrenuitycrm")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    await db.refresh(row)
    return _kpi_to_dict(row)


@router.delete("/derived/{kpi_name}", status_code=204)
async def delete_derived_kpi(kpi_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigKPI, kpi_name)
    if not row:
        raise HTTPException(404, f"KPI '{kpi_name}' not found in configkpisrenuitycrm")
    await db.delete(row)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kpi_to_dict(row: ConfigKPI) -> dict[str, Any]:
    return {
        "kpi_name": row.kpi_name,
        "measure": row.measure,
        "label": row.label,
        "format": row.format,
        "description": row.description,
        "from_table": row.from_table,
        "is_main_kpi": row.is_main_kpi,
        "pbi_measure_name": row.pbi_measure_name,
        "date_column": getattr(row, "date_column", None),
        "created_at": str(row.created_at) if row.created_at else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }
