"""ConfigSignalJob model — pre-configured signal computation jobs."""
from sqlalchemy import Column, String, Boolean, Integer, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB

from oneplatform_core.database import Base
from .crm_config_tables import CONFIG_SIGNAL_JOBS_TABLE
from .schema import SCHEMA


class ConfigSignalJob(Base):
    __tablename__ = CONFIG_SIGNAL_JOBS_TABLE
    __table_args__ = {"schema": SCHEMA}

    job_id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String(200), nullable=True, default="")
    pbi_measure_name = Column(String(500), nullable=True)
    kpi_name = Column(String(100), nullable=False, index=True)
    dimensions = Column(JSONB, nullable=False)
    features = Column(JSONB, nullable=False)
    signals = Column(JSONB, nullable=False)
    filters = Column(JSONB, nullable=True)
    feature_params = Column(JSONB, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
