"""ConfigKPIDependency model — causal chains for the Why Engine."""
from sqlalchemy import Column, String, Integer, Text, DateTime, func

from platform_core.database import Base
from .portal_config_tables import CONFIG_KPI_DEPENDENCIES_TABLE
from .schema import SCHEMA


class ConfigKPIDependency(Base):
    __tablename__ = CONFIG_KPI_DEPENDENCIES_TABLE
    __table_args__ = {"schema": SCHEMA}

    dependency_id = Column(Integer, primary_key=True, autoincrement=True)
    parent_kpi = Column(String(100), nullable=False, index=True)
    dependent_kpi = Column(String(100), nullable=False)
    label = Column(String(300), nullable=False)
    rationale = Column(Text, nullable=False)
    sort_order = Column(Integer, nullable=True, default=0)
    pbi_measure_name = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
