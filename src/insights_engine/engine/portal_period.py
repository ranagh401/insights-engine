"""Portal reporting windows — fixed ``main_insights.period`` labels only.

Monthly: ``1-30 Apr``
Weekly: ``22-28 Jun`` (Portal default week; was ``3-9 May`` on Client)
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

PeriodBucket = str  # "monthly" | "weekly"


class InsightPeriodBucket(str, Enum):
    monthly = "monthly"
    weekly = "weekly"


class PortalPeriodType(str, Enum):
    """Query param for portal APIs — choose one window or both (default)."""

    monthly = "monthly"
    weekly = "weekly"


PORTAL_MONTHLY_PERIOD_LABEL = "1-30 Apr"

# Portal portal fallback when main_insights has no run (weekly only).
PORTAL_WEEKLY_DEFAULT_LABEL = "22-28 Jun"
PORTAL_WEEKLY_DEFAULT_START = date(2026, 6, 22)
PORTAL_WEEKLY_DEFAULT_END = date(2026, 6, 28)

PORTAL_WEEKLY_PERIOD_LABEL = PORTAL_WEEKLY_DEFAULT_LABEL

PORTAL_EXECUTIVE_SUMMARY_MAX_WORDS = 40
PORTAL_EXECUTIVE_SUMMARY_POINTER_COUNT = 5

# Fixed executive summary (no LLM) — week of May 3–9 / April 1–30.
PORTAL_EXECUTIVE_SUMMARY_POINTERS_WEEKLY: tuple[str, ...] = (
    "C.Mad City demo rate fell 11pp to 77% vs 88% 4-week rolling average.",
    "B.Statewide revenue dropped 30% week-over-week; set rate fell to 25%.",
    "G.West rep count down 72% week-over-week; demo rate fell 14pp to 80%.",
    "Emerging closed deals plunged 74% week-over-week after 81% spend cut.",
    "Self-Generated set rate dropped 17pp week-over-week; close rate compressed to 43%.",
)

PORTAL_EXECUTIVE_SUMMARY_POINTERS_MONTHLY: tuple[str, ...] = (
    "A.East Gulf gross close rate fell from 34% 3-month rolling average to 20% in April.",
    "Established cost per demo rose 18% from $494 in March to $581 in April.",
    "Emerging cost per lead rose 114% from $20.30 3-month rolling average in April.",
    "H.Rite Window cost per demo surged 48% from $577 3-month rolling average in April.",
    "Media deal count up 212% month-over-month but ticket fell 16% to $17,920 in April.",
)


def _limit_executive_words(text: str, max_words: int) -> str:
    words = [w for w in (text or "").split() if w]
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def executive_summary_pointers_for_period(period_label: str) -> list[str]:
    """Return five CFO-style bullets with plain-English period for weekly or monthly window."""
    if period_label == PORTAL_MONTHLY_PERIOD_LABEL:
        source = PORTAL_EXECUTIVE_SUMMARY_POINTERS_MONTHLY
    else:
        source = PORTAL_EXECUTIVE_SUMMARY_POINTERS_WEEKLY
    cleaned = [
        _limit_executive_words(str(p).strip(), PORTAL_EXECUTIVE_SUMMARY_MAX_WORDS)
        for p in source
        if str(p).strip()
    ]
    while len(cleaned) < PORTAL_EXECUTIVE_SUMMARY_POINTER_COUNT:
        cleaned.append("No additional cross-cutting theme identified.")
    return cleaned[:PORTAL_EXECUTIVE_SUMMARY_POINTER_COUNT]


def portal_period_label_for_bucket(bucket: InsightPeriodBucket) -> str:
    if bucket == InsightPeriodBucket.monthly:
        return PORTAL_MONTHLY_PERIOD_LABEL
    return PORTAL_WEEKLY_PERIOD_LABEL


def _parse_row_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def filter_rows_by_period_label(
    rows: list[dict[str, Any]],
    period_label: str,
) -> list[dict[str, Any]]:
    """Keep only rows whose ``period`` matches exactly (trimmed)."""
    want = str(period_label).strip()
    return [r for r in rows if str(r.get("period") or "").strip() == want]


def split_portal_insight_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split insights into portal monthly (``1-30 Apr``) and weekly (Portal ``22-28 Jun``) buckets."""
    monthly = filter_rows_by_period_label(rows, PORTAL_MONTHLY_PERIOD_LABEL)
    weekly = filter_rows_by_period_label(rows, PORTAL_WEEKLY_PERIOD_LABEL)
    return monthly, weekly


def period_window_from_rows(
    rows: list[dict[str, Any]],
    *,
    period_label: str,
) -> dict[str, Any] | None:
    """Derive label + start/end dates from rows for one fixed portal period."""
    filtered = filter_rows_by_period_label(rows, period_label)
    if not filtered:
        return None
    starts: list[date] = []
    ends: list[date] = []
    for r in filtered:
        ps = _parse_row_date(r.get("period_start"))
        pe = _parse_row_date(r.get("period_end"))
        if ps:
            starts.append(ps)
        if pe:
            ends.append(pe)
    return {
        "period": period_label,
        "period_start": min(starts).isoformat() if starts else None,
        "period_end": max(ends).isoformat() if ends else None,
    }


def default_portal_period_window(bucket: PortalPeriodType) -> dict[str, Any] | None:
    """Fixed Portal windows when main_insights cannot supply dates."""
    if bucket == PortalPeriodType.weekly:
        return {
            "period": PORTAL_WEEKLY_DEFAULT_LABEL,
            "period_start": PORTAL_WEEKLY_DEFAULT_START.isoformat(),
            "period_end": PORTAL_WEEKLY_DEFAULT_END.isoformat(),
        }
    return None


def resolve_portal_period_window(
    bucket: PortalPeriodType,
    monthly_win: dict[str, Any] | None,
    weekly_win: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """Prefer main_insights window; fall back to Portal defaults (weekly only)."""
    win = monthly_win if bucket == PortalPeriodType.monthly else weekly_win
    if win:
        start, end = parse_period_window_dates(win)
        if start is not None and end is not None:
            return win, "main_insights"
    fallback = default_portal_period_window(bucket)
    if fallback is not None:
        return fallback, "default"
    label_hint = (
        PORTAL_MONTHLY_PERIOD_LABEL
        if bucket == PortalPeriodType.monthly
        else PORTAL_WEEKLY_PERIOD_LABEL
    )
    raise ValueError(
        f"Could not resolve {bucket.value} period '{label_hint}' "
        "in main_insights and no default is configured."
    )


def parse_period_window_dates(window: dict[str, Any] | None) -> tuple[date | None, date | None]:
    if not window:
        return None, None
    start_raw = window.get("period_start")
    end_raw = window.get("period_end")
    try:
        start_d = date.fromisoformat(str(start_raw)[:10]) if start_raw else None
        end_d = date.fromisoformat(str(end_raw)[:10]) if end_raw else None
        return start_d, end_d
    except ValueError:
        return None, None


def _executive_summary_bucket(
    rows: list[dict[str, Any]],
    *,
    period_label: str,
) -> dict[str, Any]:
    """Period metadata + fixed portal executive pointers (no LLM)."""
    scoped = filter_rows_by_period_label(rows, period_label)
    window = period_window_from_rows(scoped, period_label=period_label)
    return {
        "period": (window or {}).get("period") or period_label,
        "period_start": (window or {}).get("period_start"),
        "period_end": (window or {}).get("period_end"),
        "insight_count": len(scoped),
        "pointers": executive_summary_pointers_for_period(period_label),
    }


async def fetch_portal_executive_summary_split(
    store: Any,
    *,
    run_timestamp: Optional[datetime] = None,
    limit: int = 500,
    period_type: str | None = None,
) -> dict[str, Any]:
    """Return fixed 5-pointer executive summary for portal monthly / weekly windows."""
    if run_timestamp is not None:
        ts_used = run_timestamp
    else:
        ts_used = await store.get_latest_main_insight_run_timestamp()
    if not ts_used:
        return {"run_timestamp": None, "monthly": None, "weekly": None}

    rows = await store.list_main_insight_rows(
        run_timestamp=ts_used,
        limit=max(1, min(limit, 5000)),
        pascal_case=False,
    )
    monthly_rows, weekly_rows = split_portal_insight_rows(rows)

    want_monthly = period_type in (None, InsightPeriodBucket.monthly.value)
    want_weekly = period_type in (None, InsightPeriodBucket.weekly.value)
    if period_type and period_type not in (
        InsightPeriodBucket.monthly.value,
        InsightPeriodBucket.weekly.value,
    ):
        raise ValueError(
            f"period_type must be '{InsightPeriodBucket.monthly.value}' or "
            f"'{InsightPeriodBucket.weekly.value}'"
        )

    monthly = None
    weekly = None
    if want_monthly:
        monthly = _executive_summary_bucket(
            monthly_rows, period_label=PORTAL_MONTHLY_PERIOD_LABEL
        )
    if want_weekly:
        weekly = _executive_summary_bucket(
            weekly_rows, period_label=PORTAL_WEEKLY_PERIOD_LABEL
        )
    return {
        "run_timestamp": ts_used,
        "monthly": monthly,
        "weekly": weekly,
    }
