"""ConfigSignal model — one row per signal definition."""
from sqlalchemy import Column, String, Float, Integer, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB

from platform_core.database import Base
from .crm_config_tables import CONFIG_SIGNALS_TABLE
from .schema import SCHEMA


class ConfigSignal(Base):
    __tablename__ = CONFIG_SIGNALS_TABLE
    __table_args__ = {"schema": SCHEMA}

    signal_name = Column(String(100), primary_key=True)
    feature_name = Column(String(100), nullable=False)
    operator = Column(String(20), nullable=False)
    threshold = Column(Float, nullable=False)
    # threshold2: upper bound for 'between' operator (threshold <= value <= threshold2)
    threshold2 = Column(Float, nullable=True)
    severity = Column(String(20), nullable=False)
    message_template = Column(Text, nullable=False)
    top_n = Column(Integer, nullable=True)
    # format: 'percentage' → threshold/feature value is a %; 'absolute' → threshold is raw KPI units
    format = Column(String(20), nullable=False, server_default='percentage')
    # dimensions: optional per-dimension threshold overrides, e.g. {"brand": -10.0, "region": -25.0}
    dimensions = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
