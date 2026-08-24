"""Per-KPI / per-dimension threshold overrides for signal detection.

Rows here are evaluated before ``config_signals``. If no matching row
exists for a (kpi_name, dimension_name) pair, ``ConfigLoader`` falls back
to signal definitions loaded via active jobs and ``config_signals``.
"""
from sqlalchemy import Column, Float, Integer, String, DateTime, func

from platform_core.database import Base
from .schema import SCHEMA


class ConfigSignalThreshold(Base):
    __tablename__ = "config_signal_threshold"
    __table_args__ = {"schema": SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    kpi_name = Column(String(100), nullable=False, index=True)
    # NULL = wildcard (applies to any dimension for this KPI when no exact match)
    dimension_name = Column(String(100), nullable=True, index=True)
    operator = Column(String(20), nullable=False)
    threshold = Column(Float, nullable=False)
    severity = Column(String(20), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
