"""ConfigDimension model — maps to insights.config_dimensionsreunityportal."""
from sqlalchemy import Column, String, Integer, Text, DateTime, func

from platform_core.database import Base
from .portal_config_tables import CONFIG_DIMENSIONS_TABLE
from .schema import SCHEMA


class ConfigDimension(Base):
    __tablename__ = CONFIG_DIMENSIONS_TABLE
    __table_args__ = {"schema": SCHEMA}

    dimension_id = Column(Integer, primary_key=True, autoincrement=True)
    dimension_name = Column("dimensionname", String(100), nullable=False, unique=True)
    select_clause = Column("selectclause", Text, nullable=False)
    join_clause = Column("joinclause", Text, nullable=True, default="")
    group_by_clause = Column("groupbyclause", Text, nullable=False)
    order_by_clause = Column("orderbyclause", Text, nullable=False)
    description = Column(String(500), nullable=True, default="")
    table_alias = Column(String(200), nullable=True)
    dimension_table = Column(String(200), nullable=True)
    pbi_table_name = Column(String(200), nullable=True)
    pbi_column_name = Column(String(200), nullable=True)
    created_at = Column("createdat", DateTime(timezone=True), server_default=func.now())
    updated_at = Column("updatedat", DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
