"""DAX engine settings for the Power BI analytics approach."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"


class LlmSettings(BaseSettings):
    """LLM credentials for main-insight narratives (Azure OpenAI + optional Foundry Claude)."""

    PLATFORM_OPENAI__API_KEY: str
    PLATFORM_OPENAI__ENDPOINT: str
    PLATFORM_OPENAI__DEPLOYMENT: str
    PLATFORM_OPENAI__API_VERSION: str = "2024-02-15-preview"
    PLATFORM_OPENAI__TEMPERATURE: float = 0.2
    # Secondary Azure OpenAI (e.g. GPT-5.x mini on another resource). Used when ``main_insights_llm=azure_gpt54_mini``.
    INSIGHTS_AZURE_GPT54_MINI__API_KEY: str = ""
    INSIGHTS_AZURE_GPT54_MINI__ENDPOINT: str = ""
    INSIGHTS_AZURE_GPT54_MINI__DEPLOYMENT: str = ""
    INSIGHTS_AZURE_GPT54_MINI__API_VERSION: str = "2024-12-01-preview"
    # Azure AI Models API — Cohere Command A+ (``main_insights_llm=cohere_command_a_plus``).
    INSIGHTS_COHERE_AZURE__API_KEY: str = ""
    INSIGHTS_COHERE_AZURE__ENDPOINT: str = "https://eastus.api.cognitive.microsoft.com/"
    INSIGHTS_COHERE_AZURE__API_VERSION: str = "2024-05-01-preview"
    INSIGHTS_COHERE_AZURE__MODEL: str = "Cohere-command-a-plus-05-2026"
    # How many signal clusters may run narrative LLM work concurrently (bounded semaphore).
    # Kept moderate by default: effective load is this × chunk × WHY concurrency — raises 429 if too high.
    INSIGHTS_MAIN_INSIGHTS_LLM_CONCURRENCY: int = 2
    # Parallel **narrative JSON** chunk calls **within one** large cluster (decoupled from the above).
    INSIGHTS_NARRATIVE_CHUNK_LLM_CONCURRENCY: int = 4
    # Parallel **WHY** sub-chunk LLM calls inside ``_llm_narrate_why_lines_chunked`` (main-insight + alpha).
    INSIGHTS_WHY_NARRATIVE_LLM_CONCURRENCY: int = 3
    # Retries in ``_call_gpt4o`` for 429 / transient API errors (SDK default retries are disabled; we back off here).
    INSIGHTS_LLM_MAX_ATTEMPTS: int = 8
    INSIGHTS_LLM_RATE_LIMIT_BASE_WAIT: float = 4.0
    INSIGHTS_LLM_RATE_LIMIT_MAX_WAIT: float = 120.0
    # Base directory for LLM prompt dumps. If empty, InsightEngine defaults to ``<repo>/prompt_logs``.
    # Writes go to ``<dir>/clustering/`` and ``<dir>/main_insight/``.
    INSIGHTS_PROMPT_LOG_DIR: str = ""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )


class Settings(BaseSettings):
    AZURE_TENANT_ID: str
    AZURE_CLIENT_ID: str
    AZURE_CLIENT_SECRET: str
    PBI_WORKSPACE_ID: str
    PBI_DATASET_ID: str
    POSTGRES_DSN: str
    DATE_TABLE_NAME: str = "Calender"
    DATE_COLUMN_NAME: str = "Calender Date"
    # POST /pbi/data/fetch: when dimension ``week`` is used, add three extra group columns
    # from this table (defaults to the week dimension's PBI table if empty). Leave column
    # names empty to skip enrichment (avoids DAX 400 if your model uses other names).
    INSIGHTS_FETCH_WEEK_TABLE: str = ""
    INSIGHTS_FETCH_WEEK_SORT_COLUMN: str = ""
    INSIGHTS_FETCH_WEEK_START_COLUMN: str = ""
    INSIGHTS_FETCH_WEEK_END_COLUMN: str = ""
    WHY_ANALYSIS_SWEEP_MINUTES: int = 5
    PBI_MAX_CONCURRENT_QUERIES: int = 5
    PBI_RETRY_MAX_ATTEMPTS: int = 3
    # Bulk WHY DAX can exceed 120s on large models; httpx read timeout (seconds).
    PBI_DAX_READ_TIMEOUT_SECONDS: float = 600.0
    # Physical table for narrative insight rows (schema ``insights``). CRM default: maininsightscrm.
    MAIN_INSIGHTS_TABLE: str = "maininsightscrm"

    # WHY sweep batching (bounded RAM / DAX member lists; mirrors batch flush pattern
    # used in ms-insights-portal SignalEngine.compute_whys for Databricks).
    INSIGHTS_WHY_DB_FETCH_BATCH: int = 5000
    INSIGHTS_WHY_CACHE_ENTITY_BATCH: int = 400

    # Azure OpenAI configs
    PLATFORM_OPENAI__API_KEY: str
    PLATFORM_OPENAI__ENDPOINT: str
    PLATFORM_OPENAI__DEPLOYMENT: str
    PLATFORM_OPENAI__API_VERSION: str = "2024-02-15-preview"
    PLATFORM_OPENAI__TEMPERATURE: float = 0.2

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_llm_settings() -> LlmSettings:
    return LlmSettings()


@lru_cache()
def get_dax_settings() -> Settings:
    return Settings()
