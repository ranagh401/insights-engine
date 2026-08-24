"""Human-readable formatting for KPI values in LLM prompts and digests.

Power BI often returns *rates* as fractions (e.g. ``0.21`` for 21%). Names containing
``rate`` (word-boundary), ``percent``, ``pct``, etc. are shown as percentages with at
most two decimal places. Other metrics use plain decimals (max two fractional digits).
"""

from __future__ import annotations

import re
from typing import Optional

# KPIs stored as rates: period-over-period feature uses **percentage-point** difference (A−B on % scale),
# not relative (a−b)/b. See ``kpi_uses_percentage_point_difference``.
_PERCENTAGE_POINT_DIFFERENCE_KPIS = frozenset(
    {
        "set_rate",
        "issue_rate",
        "demo_rate",
        "gross_close_rate",
        "net_close_rate",
    },
)


def _norm_kpi_key(name: str) -> str:
    return re.sub(r"\s+", "_", (name or "").strip().lower())


def kpi_uses_percentage_point_difference(kpi_name: str) -> bool:
    """True when WoW/MoM-style growth for this KPI is computed as A−B in percentage points."""
    return _norm_kpi_key(kpi_name) in _PERCENTAGE_POINT_DIFFERENCE_KPIS


def kpi_storage_to_percent_point_scale(x: float) -> float:
    """Map stored KPI level to 0–100 percent scale (same rules as ``format_metric_value_for_display``)."""
    xf = float(x)
    if abs(xf) <= 1.0000001:
        return xf * 100.0
    return xf

_RATIO_RE = re.compile(r"\bratio\b", re.I)


def metric_name_implies_percent_display(name: str) -> bool:
    """True when the metric label suggests values are rates / share / percent scale."""
    n = (name or "").strip()
    if not n:
        return False
    ml = n.lower()
    if "%" in ml:
        return True
    if "percent" in ml or "percentage" in ml or "pct" in ml:
        return True
    # "rate" substring covers ``Net Close Rate``, ``demo_rate``, etc. (not ``strategy``).
    if "rate" in ml:
        return True
    if _RATIO_RE.search(n):
        return True
    return False


def _trim_trailing_zeros(s: str) -> str:
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".")


def format_generic_numeric(val: Optional[float], *, max_decimals: int = 2) -> str:
    """Plain number for features / observed / deltas — no % conversion; cap fractional digits."""
    if val is None:
        return "n/a"
    x = float(val)
    if x != x:  # NaN
        return "n/a"
    places = max(0, min(6, int(max_decimals)))
    s = f"{x:.{places}f}"
    return _trim_trailing_zeros(s) if s else "0"


def format_metric_value_for_display(metric_name: str, val: Optional[float], *, max_decimals: int = 2) -> str:
    """Format a KPI *level* (current / prior) for prompts: rates as ``21%`` style when appropriate."""
    if val is None:
        return "n/a"
    x = float(val)
    if x != x:
        return "n/a"
    places = max(0, min(6, int(max_decimals)))

    if metric_name_implies_percent_display(metric_name):
        # Values in (0,1] are treated as fractions; already-on-percent-scale stays as-is.
        if abs(x) <= 1.0000001:
            pct_val = x * 100.0
        else:
            pct_val = x
        s = f"{pct_val:.{places}f}"
        return _trim_trailing_zeros(s) + "%"

    s = f"{x:.{places}f}"
    out = _trim_trailing_zeros(s)
    return out if out else "0"
