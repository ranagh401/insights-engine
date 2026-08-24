"""ConfigKPIValidDimension — maps to insights.configkpivaliddimensionsclientcrm."""
from sqlalchemy import Column, String, Boolean, Integer, DateTime, func

from platform_core.database import Base
from .crm_config_tables import CONFIG_KPI_VALID_DIMENSIONS_TABLE
from .schema import SCHEMA


class ConfigKPIValidDimension(Base):
    __tablename__ = CONFIG_KPI_VALID_DIMENSIONS_TABLE
    __table_args__ = {"schema": SCHEMA}

    id = Column(Integer, primary_key=True, autoincrement=True)
    kpi_name = Column("kpiname", String(100), nullable=False, index=True)
    dimension_name = Column("dimensionname", String(100), nullable=False)
    is_valid = Column(Boolean, nullable=False, default=True)
    reason = Column(String(500), nullable=True)
    created_at = Column("createdat", DateTime(timezone=True), server_default=func.now())
