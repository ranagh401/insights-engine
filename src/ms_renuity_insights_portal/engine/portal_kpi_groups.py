"""KPI groups for the portal executive summary.

The frontend passes a ``group`` (e.g. ``group1``) when a KPI card is clicked;
the executive summary is then generated only from insights whose ``kpi`` column
names any KPI in that group. No group -> all KPIs (overall summary).

Generated from configkpisrenuitycrm KPI1..KPI5 group flags. A row in
maininsightscrm matches a group if ANY of its (comma-separated) KPIs is in the
group set. A KPI may belong to multiple groups (e.g. real_pipeline_value).
"""

from __future__ import annotations

# group_id -> list of KPI names (kpiname slugs in configkpisrenuitycrm)
PORTAL_KPI_GROUPS: dict[str, list[str]] = {
    # Card 1 — Real vs Stated Pipeline / movement by stage & week
    "group1": [
        "avg_deal_size",
        "stage_moved",
        "sales_rep_count",
        "real_pipeline_value",
        "stated_pipeline_value",
        "real_pct",
        "deals_moving_count",
        "deals_not_moving_count",
        "deals_count_with_movement_recorded_in_1_week",
        "deals_count_with_movement_recorded_in_2_weeks",
        "deals_count_with_movement_recorded_in_3_weeks",
        "deals_count_with_movement_recorded_in_4_weeks",
        "deals_count_with_movement_recorded_in_5_weeks",
        "deals_count_with_movement_recorded_in_6_weeks",
        "deals_count_with_movement_recorded_in_7_weeks",
        "deals_count_with_movement_recorded_in_8_plus_weeks",
        "deals_amount_with_movement_recorded_in_1_week",
        "deals_amount_with_movement_recorded_in_2_weeks",
        "deals_amount_with_movement_recorded_in_3_weeks",
        "deals_amount_with_movement_recorded_in_4_weeks",
        "deals_amount_with_movement_recorded_in_5_weeks",
        "deals_amount_with_movement_recorded_in_6_weeks",
        "deals_amount_with_movement_recorded_in_7_weeks",
        "deals_amount_with_movement_recorded_in_8_plus_weeks",
        "days_since_last_moved",
        "high_value_and_current_month_commit_flag",
        "advancing_deals_count",
        "advancing_deals_amount",
        "slipping_deals_count",
        "slipping_deals_amount",
    ],
    # Card 2 — Infant Mortality / dead leads by rep & source
    "group2": [
        "real_pipeline_value",
        "dead_leads_count",
        "dead_leads_amount",
        "worst_rep_by_dead_leads_amount",
        "worst_rep_dead_amount",
        "worst_rep_dead_count",
        "alive_leads_count",
        "alive_leads_amount",
        "dead_pct_by_count",
        "inbound_dead_leads_count",
        "event_dead_leads_count",
        "referral_dead_leads_count",
        "sales_organic_dead_leads_count",
        "demand_gen_dead_leads_count",
        "oem_dead_leads_count",
        "webinar_dead_leads_count",
        "demand_gen_dead_leads_amount",
        "event_dead_leads_amount",
        "inbound_dead_leads_amount",
        "oem_dead_leads_amount",
        "referral_dead_leads_amount",
        "sales_organic_dead_leads_amount",
        "webinar_dead_leads_amount",
    ],
    # Card 3 — Organic vs Referral Sourcing
    "group3": [
        "real_pipeline_value",
        "referral_sourced_leads_count",
        "referral_sourced_leads_amount",
        "referral_sourced_leads_pct_by_count",
        "organic_sourced_leads_count",
        "organic_sourced_leads_amount",
        "organic_sourced_leads_pct_by_count",
    ],
    # Card 4 — Forecast Smoke / commit
    "group4": [
        "commit_slipped_count",
        "current_month_commit_count",
        "next_month_commit_count",
        "previous_month_commit_amount",
        "next_month_commit_amount",
        "commit_slip_history",
        "commit_month",
        "slipping_deals_count",
        "slipping_deals_amount",
    ],
    # Card 5 — OEM Funding Left on Table
    "group5": [
        "approved_funding_amount",
        "untapped_funding_deals_amount",
        "approved_funding_deals_count",
        "untapped_funding_deals_count",
    ],
}


def resolve_group_kpis(group: str | None) -> set[str] | None:
    """Return the KPI set for a group id, or None for 'all KPIs' (overall)."""
    if group is None:
        return None
    key = group.strip().lower()
    if not key:
        return None
    if key not in PORTAL_KPI_GROUPS:
        raise KeyError(group)
    return set(PORTAL_KPI_GROUPS[key])


def row_matches_group(row: dict, group_kpis: set[str]) -> bool:
    """True if the insight row's ``kpi`` (or ``kpi_family``) names any group KPI."""
    raw = (row.get("kpi") or row.get("kpi_family") or "")
    row_kpis = {k.strip().lower() for k in str(raw).split(",") if k.strip()}
    return bool(row_kpis & group_kpis)


def groups_for_kpi(kpi: str | None) -> list[str]:
    """Every group id whose KPI set names ``kpi``. Empty when no card claims it."""
    key = (kpi or "").strip().lower()
    if not key:
        return []
    return sorted(
        g for g, kpis in PORTAL_KPI_GROUPS.items() if key in {k.lower() for k in kpis}
    )


def group_tag_for_kpis(raw: str | None) -> str | None:
    """``group_name`` tag for a row's ``kpi`` value, which may be comma-separated.

    Returns "group1", or "group1,group4" when several cards claim the KPI, or
    None when none do. The inverse of what ``row_in_group`` reads back.
    """
    found: set[str] = set()
    for part in str(raw or "").split(","):
        found.update(groups_for_kpi(part))
    return ",".join(sorted(found)) or None


def _norm_group(value: str) -> str:
    """Normalize a group tag: lowercase, drop spaces (so 'Group 1' == 'group1')."""
    return "".join(str(value).split()).lower()


def row_in_group(row: dict, group: str) -> bool:
    """True if the row's manual ``group_name`` column names ``group``.

    ``group_name`` may hold one tag ("group1") or several ("group1,group3").
    Matching is case- and space-insensitive.
    """
    want = _norm_group(group)
    if not want:
        return False
    raw = row.get("group_name")
    if raw is None:
        return False
    tags = {_norm_group(t) for t in str(raw).split(",") if t.strip()}
    return want in tags
