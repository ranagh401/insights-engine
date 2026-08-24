"""Service configuration base types (subset used by insights portal)."""
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceDetails(BaseModel):
    """HTTP/gRPC service identity and listen addresses."""

    name: str = "insights-service"
    version: str = "1.0.0"
    port: int = 8009
    host: str = "0.0.0.0"
    grpc_port: int = 50055
    grpc_host: str = "0.0.0.0"
    description: str = ""


class DatabaseSettings(BaseModel):
    """Primary application database (PostgreSQL async URL)."""

    url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres",
        description="SQLAlchemy async URL for insights config tables",
    )


class ServiceConfig(BaseSettings):
    """Root settings shared across microservices (minimal fields for this replica)."""

    service: ServiceDetails = Field(default_factory=ServiceDetails)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    debug: bool = Field(default=False)
    environment: str = Field(default="development")

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_prefix="PLATFORM_",
        extra="ignore",
    )
