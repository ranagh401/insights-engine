"""Portal PostgreSQL config table names (``insights`` schema).

Physical tables use the ``*clientportal`` suffix — separate from shared Client config.
"""

CONFIG_KPIS_TABLE = "configkpisclientportal"
CONFIG_DERIVED_KPIS_TABLE = "configderivedkpisclientportal"
CONFIG_DIMENSIONS_TABLE = "config_dimensionsreunityportal"
CONFIG_KPI_VALID_DIMENSIONS_TABLE = "configkpivaliddimensionsclientportal"
CONFIG_KPI_DEPENDENCIES_TABLE = "configkpidependenciesclientportal"
CONFIG_SIGNALS_TABLE = "config_signalsclientportal"
CONFIG_FEATURES_TABLE = "config_featuresclientportal"
CONFIG_SIGNAL_JOBS_TABLE = "config_signaljobsclientportal"
