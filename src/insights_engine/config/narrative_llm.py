"""Main-insight narrative LLM selection (OpenAPI / Swagger dropdown)."""

from enum import Enum


class MainInsightsNarrativeModel(str, Enum):
    """Which deployment backs one-shot main insight JSON (standard + dimensional)."""

    azure_default = "azure_default"
    azure_gpt54_mini = "azure_gpt54_mini"
    cohere_command_a_plus = "cohere_command_a_plus"
