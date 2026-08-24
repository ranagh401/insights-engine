"""Fallback analysis dates from stored signal DAX (``signal_log``).

Primary WHY windows come from ``config_signaljobsclientcrm`` via
``SignalJobConfig.period_start`` / ``period_end`` (``filters.period``). When those
are missing or the job row cannot be resolved, parse the first
``CALENDAR(DATE(y,m,d), DATE(y,m,d))`` in ``dax_kpi_query`` / ``dax_feature_query``
so drill-down still aligns with the alert.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

from ..config.models import Signal

logger = logging.getLogger(__name__)

# First CALENDAR(DATE(...), DATE(...)) in the blob = current period for standard templates.
_CALENDAR_DATE_RANGE = re.compile(
    r"CALENDAR\s*\(\s*DATE\s*\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*,\s*"
    r"DATE\s*\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def parse_first_calendar_bounds_from_dax(dax: str | None) -> tuple[Optional[date], Optional[date]]:
    """Return ``(period_start, period_end)`` from the **first** ``CALENDAR(DATE, DATE)`` match."""
    if not dax or not dax.strip():
        return None, None
    m = _CALENDAR_DATE_RANGE.search(dax)
    if not m:
        return None, None
    try:
        y1, mo1, d1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y2, mo2, d2 = int(m.group(4)), int(m.group(5)), int(m.group(6))
        start = date(y1, mo1, d1)
        end = date(y2, mo2, d2)
        if end < start:
            start, end = end, start
        return start, end
    except ValueError:
        logger.debug("signal_dax_period: invalid DATE in CALENDAR snippet", exc_info=True)
        return None, None


def why_period_bounds_from_signal(sig: Signal) -> tuple[Optional[date], Optional[date]]:
    """Prefer ``dax_kpi_query``, then ``dax_feature_query``, for period bounds."""
    for blob in (sig.dax_kpi_query, sig.dax_feature_query):
        ps, pe = parse_first_calendar_bounds_from_dax(blob)
        if ps is not None and pe is not None:
            return ps, pe
    return None, None
