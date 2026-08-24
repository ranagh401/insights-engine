"""ConfigFeature model — maps to insights.config_featuresclientportal."""
from sqlalchemy import Column, String, Text, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB

from platform_core.database import Base
from .portal_config_tables import CONFIG_FEATURES_TABLE
from .schema import SCHEMA


class ConfigFeature(Base):
    __tablename__ = CONFIG_FEATURES_TABLE
    __table_args__ = {"schema": SCHEMA}

    feature_name = Column(String(100), primary_key=True)
    function_name = Column(String(100), nullable=False)
    label = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True, default="")
    requires_time_dimension = Column(Boolean, nullable=False, default=False)
    default_params = Column(JSONB, nullable=True, default=dict)
    format = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
