"""
Configuration for the insights microservice.

This module follows the shared ServiceConfig pattern used by other services.
"""
from functools import lru_cache
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from oneplatform_core.auth.jwt import JWTManager
from oneplatform_core.config.settings import create_service_config, find_project_root
from oneplatform_core.config.validation import ServiceConfig, ServiceDetails
from oneplatform_core.observability import logger

async def get_cache():
    """Return ``None`` so callers fall back to non-cached behavior."""
    return None
class DatabricksConfig(BaseModel):
    """Databricks SQL Warehouse connection settings."""

    host: str = Field(default="", description="Databricks workspace URL")
    token: str = Field(default="", description="Databricks PAT token")
    warehouse_id: str = Field(default="", description="SQL Warehouse ID")
    catalog: str = Field(default="sales_detail", description="Unity Catalog name")
    schema_name: str = Field(default="renuity_insights_portal", description="Schema/database name")
    # Optional: table that maps date_id (1–105) to actual dates so PeriodStart/PeriodEnd can be set.
    # Table must have: date_id (INT), and either (period_start_date, period_end_date) or full_date.
    date_id_mapping_table: Optional[str] = Field(default=None, description="e.g. dim_period — maps date_id to calendar dates")

    @property
    def namespace(self) -> str:
        return f"{self.catalog}.{self.schema_name}"


class SqlServerConfig(BaseModel):
    """SQL Server (AdventureWorks) connection settings."""

    conn_str: str = Field(default="", description="ODBC connection string")


class OpenAIConfig(BaseModel):
    """Azure OpenAI configuration for LLM insights."""

    api_key: str = Field(default="", description="OpenAI API key")
    endpoint: str = Field(default="", description="Azure OpenAI endpoint URL")
    api_version: str = Field(default="2024-06-01", description="API version")
    deployment: str = Field(default="gpt-4o-mini", description="Model deployment name")
    temperature: float = Field(default=0.2, description="LLM temperature")


class InsightsServiceConfig(ServiceConfig):
    """
    Root configuration for the insights service.

    Inherits the shared ServiceConfig used by other microservices.
    """

    service: ServiceDetails = Field(
        default_factory=lambda: ServiceDetails(
            name="insights-service",
            version="1.0.0",
            port=8010,
            host="0.0.0.0",
            grpc_port=50055,
            grpc_host="0.0.0.0",
            description="Signal detection, KPI analysis, and LLM-powered insights",
        )
    )

    databricks: DatabricksConfig = Field(default_factory=DatabricksConfig)
    sqlserver: SqlServerConfig = Field(default_factory=SqlServerConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)

    # When ConfigSignals.TopN is NULL, cap rows per (period × job × signal) to this many
    # (sorted by feature value: worst first for lt/lte operators). None = no cap.
    signal_default_top_n: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional cap on triggered rows per signal task when DB TopN is unset",
    )

    model_config = SettingsConfigDict(
        env_file=find_project_root() / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="ONEPLATFORM_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    async def get_jwt_manager(self) -> JWTManager:
        """Return a JWTManager instance using the shared cache backend when available."""
        cache = None
        try:
            cache = await get_cache()
        except Exception:
            logger.debug("Cache not available for JWTManager", exc_info=True)
        return JWTManager(self, cache=cache if cache is not None else None)


@lru_cache()
def get_settings() -> InsightsServiceConfig:
    """Load and cache application settings using the shared config factory."""
    logger.info("Loading application settings...")
    settings = create_service_config(InsightsServiceConfig)
    logger.info(
        "Settings loaded",
        service=settings.service.name,
        env=getattr(settings, "environment", "development"),
    )
    return settings


def get_jwt_manager() -> JWTManager:
    """Provide a JWTManager wired to the shared cache and settings."""
    settings = get_settings()
    return JWTManager(settings)
