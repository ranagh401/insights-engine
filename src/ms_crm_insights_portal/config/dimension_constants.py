"""Reserved dimension names for portfolio-level (no drill) signal jobs."""

from __future__ import annotations

# Use as ``config_signaljobsclientcrm.dimensions`` entry — no ``config_dimensions`` row required.
SELF_DIMENSION_NAME = "SELF"
# Stored on ``signal_log`` when the job runs without a business dimension.
SELF_DIMENSION_VALUE = "ALL"

_SELF_FEATURE_NAMES = frozenset({"self_kpi_value"})


def is_self_feature(feature_name: str | None) -> bool:
    """True when the feature is the portfolio raw-KPI value (SELF jobs)."""
    if not feature_name:
        return False
    norm = feature_name[len("calculate_") :] if feature_name.startswith("calculate_") else feature_name
    return norm in _SELF_FEATURE_NAMES

_SELF_ALIASES = frozenset(
    {
        "SELF",
        "_SELF",
        "ALL",
        "(ALL)",
        "PORTFOLIO",
        "TOTAL",
    }
)


def is_self_dimension(dimension_name: str | None) -> bool:
    """True when the job should evaluate KPI totals with no SUMMARIZECOLUMNS group-by."""
    return (dimension_name or "").strip().upper() in _SELF_ALIASES
