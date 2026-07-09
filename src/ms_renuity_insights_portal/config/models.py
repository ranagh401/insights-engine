"""Pydantic domain models for the DAX-based KPI analytics engine."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class ConfigIncompleteError(Exception):
    """Raised when a config table row is missing required PBI mapping columns."""


class DimensionRef(BaseModel):
    dimension_name: str
    pbi_table_name: str
    pbi_column_name: str

    @property
    def pbi_expression(self) -> str:
        return f"'{self.pbi_table_name}'[{self.pbi_column_name}]"

    @property
    def treatas_ref(self) -> str:
        return self.pbi_expression


class DependencyConfig(BaseModel):
    dependency_kpi_name: str
    pbi_measure_name: Optional[str] = None


class KPIConfig(BaseModel):
    kpi_name: str
    pbi_measure_name: str
    kpi_format: str = "number"
    valid_dimensions: list[DimensionRef]
    dependencies: list[DependencyConfig] = Field(default_factory=list)


def resolve_job_filter_dimension(kpi_config: KPIConfig, dim_name: str) -> Optional[DimensionRef]:
    """Resolve a ``SignalJobConfig.filter_conditions`` key to a KPI drill dimension."""
    for d in kpi_config.valid_dimensions:
        if d.dimension_name == dim_name:
            return d
    return None


class ThresholdConfig(BaseModel):
    kpi_name: str
    dimension_name: str
    operator: str
    threshold_value: float
    severity: str


class SignalJobConfig(BaseModel):
    job_id: str
    kpi_name: str
    dimension_name: str
    features: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    frequency_minutes: int = 60
    filter_conditions: dict[str, list[str]] = Field(default_factory=dict)
    is_active: bool = True
    period_start: Optional[date] = None
    period_end: Optional[date] = None


class FeatureConfig(BaseModel):
    feature_name: str
    kpi_name: str
    dimension_name: str
    column_alias_map: dict[str, str] = Field(default_factory=dict)


class KPIRow(BaseModel):
    dimension_values: dict[str, str]
    kpi_name: str
    kpi_value: Optional[float] = None
    grain: str
    period_start: date
    period_end: date
    computed_at: datetime
    result_type: str = "kpi"
    dependency_kpi_name: Optional[str] = None


class SignalDefinition(BaseModel):
    """One row from config_signalsrenuitycrm."""
    signal_name: str
    feature_name: str
    operator: str
    threshold: float
    threshold2: Optional[float] = None
    severity: str
    message_template: str = ""
    format: str = "percentage"


class Signal(BaseModel):
    signal_id: str
    kpi_name: str
    dimension: str
    dimension_value: str
    signal_name: str = ""
    feature_name: str = ""
    feature_value: float = 0.0
    current_kpi_value: Optional[float] = None
    prev_kpi_value: Optional[float] = None
    observed_value: float
    threshold_value: float
    operator: str
    severity: str
    breach_delta: float
    detected_at: datetime
    why_computed: bool = False
    job_id: str
    dax_kpi_query: Optional[str] = None
    dax_feature_query: Optional[str] = None


class DrillDimensionResult(BaseModel):
    dimension: str
    result_type: str
    rows: list[KPIRow]
    top_contributors: list[str] = Field(default_factory=list)


class WhyRow(BaseModel):
    """One row in the redesigned ``why_results`` table."""
    why_id: Optional[str] = None
    signal_id: str
    run_timestamp: datetime
    kpi_name: str
    dimension_name: str
    dimension_value: str
    signal_name: str
    dep_kpi_name: Optional[str] = None
    dep_kpi_label: Optional[str] = None
    rationale: str = ""
    current_value: Optional[float] = None
    prev_value: Optional[float] = None
    change_pct: Optional[float] = None
    period: str = ""
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    created_at: Optional[datetime] = None


class WhyAnalysisResult(BaseModel):
    signal: Signal
    why_rows: list[WhyRow] = Field(default_factory=list)
    analysis_timestamp: datetime

class SignalCluster(BaseModel):
    """Pydantic model for a grouped cluster of signals."""
    cluster_id: Optional[str] = None
    run_timestamp: datetime
    kpi_name: str
    dimension_name: str
    dimension_value: str
    period: str
    signal_ids: str
    cluster_type: str = "alpha"
    why_inventory_json: Optional[str] = None
    created_at: Optional[datetime] = None
    #: For dimensional clusters: WHY ``period_start`` / ``period_end`` defining this bucket (weekly vs monthly).
    period_start: Optional[date] = None
    period_end: Optional[date] = None


class MainInsight(BaseModel):
    """Row written to ``insights.maininsightscrm`` (CRM default; same field order as ``vw_main_insights`` / UI grid)."""

    insight_id: Optional[str] = None
    run_timestamp: datetime
    signal_ids: str
    kpi_family: Optional[str] = None
    title: Optional[str] = None
    kpi: Optional[str] = None
    dimension_name: Optional[str] = None
    dimension_value: Optional[str] = None
    insight: str
    why: Optional[str] = None
    period: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    insight_summary: Optional[str] = None
    why_insight_summary: Optional[str] = None
    severity: Optional[str] = None
    tags: Optional[str] = None
    impact_insight: Optional[str] = None
    park: bool = False
    created_at: Optional[datetime] = None
    hook: Optional[str] = None
    like: Optional[bool] = None
    dislike: Optional[bool] = None
    remarks: Optional[str] = None
    recommended_actions: Optional[str] = None
