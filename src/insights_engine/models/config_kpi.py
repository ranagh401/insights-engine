"""ConfigKPI model — maps to insights.configkpisclientportal."""
from sqlalchemy import Column, String, Text, DateTime, Boolean, func

from platform_core.database import Base
from .portal_config_tables import CONFIG_KPIS_TABLE
from .schema import SCHEMA


class ConfigKPI(Base):
    __tablename__ = CONFIG_KPIS_TABLE
    __table_args__ = {"schema": SCHEMA}

    kpi_name = Column("kpiname", String(100), primary_key=True)
    measure = Column(Text, nullable=False)
    from_table = Column("fromtable", Text, nullable=True)
    label = Column(String(200), nullable=False)
    format = Column(String(50), nullable=False)
    description = Column(String(500), nullable=True, default="")
    date_column = Column(String(200), nullable=True)
    is_main_kpi = Column("ismainkpi", Boolean, nullable=False, default=False)
    pbi_measure_name = Column(String(500), nullable=True)

    created_at = Column("createdat", DateTime(timezone=True), server_default=func.now())
    updated_at = Column("updatedat", DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
