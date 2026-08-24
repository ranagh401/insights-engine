"""CRUD endpoints for Feature configuration (config_features)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.session import get_db
from ...models.config_feature import ConfigFeature

router = APIRouter()


class FeatureCreate(BaseModel):
    feature_name: str
    function_name: str
    label: str
    format: str
    description: Optional[str] = ""
    requires_time_dimension: bool = False
    default_params: Optional[dict] = Field(default_factory=dict)


class FeatureUpdate(BaseModel):
    function_name: Optional[str] = None
    label: Optional[str] = None
    format: Optional[str] = None
    description: Optional[str] = None
    requires_time_dimension: Optional[bool] = None
    default_params: Optional[dict] = None


@router.get("", response_model=list[dict[str, Any]])
async def list_features(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ConfigFeature).order_by(ConfigFeature.feature_name))
    rows = result.scalars().all()
    return [_to_dict(r) for r in rows]


@router.get("/{feature_name}", response_model=dict[str, Any])
async def get_feature(feature_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigFeature, feature_name)
    if not row:
        raise HTTPException(404, f"Feature '{feature_name}' not found")
    return _to_dict(row)


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_feature(body: FeatureCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.get(ConfigFeature, body.feature_name)
    if existing:
        raise HTTPException(409, f"Feature '{body.feature_name}' already exists")
    row = ConfigFeature(**body.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


@router.put("/{feature_name}", response_model=dict[str, Any])
async def update_feature(feature_name: str, body: FeatureUpdate, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigFeature, feature_name)
    if not row:
        raise HTTPException(404, f"Feature '{feature_name}' not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.flush()
    await db.refresh(row)
    return _to_dict(row)


@router.delete("/{feature_name}", status_code=204)
async def delete_feature(feature_name: str, db: AsyncSession = Depends(get_db)):
    row = await db.get(ConfigFeature, feature_name)
    if not row:
        raise HTTPException(404, f"Feature '{feature_name}' not found")
    await db.delete(row)


def _to_dict(row: ConfigFeature) -> dict[str, Any]:
    return {
        "feature_name": row.feature_name,
        "function_name": row.function_name,
        "label": row.label,
        "format": row.format,
        "description": row.description,
        "requires_time_dimension": row.requires_time_dimension,
        "default_params": row.default_params,
        "created_at": str(row.created_at) if row.created_at else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }
