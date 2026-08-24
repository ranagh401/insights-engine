"""
SQLAlchemy models for the Insights service.

All config tables live in the ``insights`` PostgreSQL schema.
"""
from .crm_config_tables import (
    CONFIG_DERIVED_KPIS_TABLE,
    CONFIG_DIMENSIONS_TABLE,
    CONFIG_FEATURES_TABLE,
    CONFIG_KPI_DEPENDENCIES_TABLE,
    CONFIG_KPI_VALID_DIMENSIONS_TABLE,
    CONFIG_KPIS_TABLE,
    CONFIG_SIGNAL_JOBS_TABLE,
    CONFIG_SIGNALS_TABLE,
)
from .schema import SCHEMA
from .config_dimension import ConfigDimension
from .config_kpi import ConfigKPI
from .config_derived_kpi import ConfigDerivedKPI
from .config_kpi_valid_dimension import ConfigKPIValidDimension
from .config_feature import ConfigFeature
from .config_signal import ConfigSignal
from .config_signal_threshold import ConfigSignalThreshold
from .config_dependency import ConfigKPIDependency
from .config_signal_job import ConfigSignalJob

__all__ = [
    "SCHEMA",
    "CONFIG_KPIS_TABLE",
    "CONFIG_DERIVED_KPIS_TABLE",
    "CONFIG_DIMENSIONS_TABLE",
    "CONFIG_KPI_VALID_DIMENSIONS_TABLE",
    "CONFIG_KPI_DEPENDENCIES_TABLE",
    "CONFIG_SIGNALS_TABLE",
    "CONFIG_FEATURES_TABLE",
    "CONFIG_SIGNAL_JOBS_TABLE",
    "ConfigDimension",
    "ConfigKPI",
    "ConfigDerivedKPI",
    "ConfigKPIValidDimension",
    "ConfigFeature",
    "ConfigSignal",
    "ConfigSignalThreshold",
    "ConfigKPIDependency",
    "ConfigSignalJob",
]
