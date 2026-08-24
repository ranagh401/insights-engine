"""CRM PostgreSQL config table names (``insights`` schema).

Physical tables use the ``*clientcrm`` suffix — separate from shared Client config.
"""

CONFIG_KPIS_TABLE = "configkpisclientcrm"
CONFIG_DERIVED_KPIS_TABLE = "configderivedkpisclientcrm"
CONFIG_DIMENSIONS_TABLE = "config_dimensionsreunitycrm"
CONFIG_KPI_VALID_DIMENSIONS_TABLE = "configkpivaliddimensionsclientcrm"
CONFIG_KPI_DEPENDENCIES_TABLE = "configkpidependenciesclientcrm"
CONFIG_SIGNALS_TABLE = "config_signalsclientcrm"
CONFIG_FEATURES_TABLE = "config_featuresclientcrm"
CONFIG_SIGNAL_JOBS_TABLE = "config_signaljobsclientcrm"
