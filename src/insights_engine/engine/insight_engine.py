"""
Insight Engine for Power BI Approach.
Uses Azure OpenAI and/or Azure AI Models (Cohere) to turn signals and WHY rows into main insights.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

import httpx
from json_repair import loads as json_repair_loads
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAzureOpenAI,
    RateLimitError,
)

from ..config.models import MainInsight, Signal, SignalCluster, WhyRow
from ..config.narrative_llm import MainInsightsNarrativeModel
from ..settings import get_llm_settings
from .frontend_text_markup import (
    apply_dimension_highlights,
    apply_frontend_bold_markup,
    normalize_body_bullets_and_markdown,
    normalize_markup_plain_text,
    normalize_why_subsection_structure,
    reformat_body_markup_only,
    reformat_title_markup_only,
    repair_broken_bold_fragments,
    strip_title_markup,
)
from .metric_display import format_generic_numeric, format_metric_value_for_display
from ..store.result_store import ResultStore

# Repo root (``ms-insights-portal-client/``) — default ``prompt_logs/`` when ``INSIGHTS_PROMPT_LOG_DIR`` is unset.
_INSIGHT_ENGINE_REPO_ROOT = Path(__file__).resolve().parents[3]

logger = logging.getLogger(__name__)

PromptLogCategory = Literal["clustering", "main_insight"]


def _write_prompt_log_txt(
    base_dir: Path,
    category: PromptLogCategory,
    context: str,
    system_prompt: str,
    user_prompt: str,
    *,
    subfolder: Optional[str] = None,
) -> None:
    """Append one request as a .txt file (full system + user messages as sent to the API).

    ``subfolder`` (e.g. ``merge``, ``narrative_chunk``) groups files under ``<base>/<category>/<subfolder>/``.
    """
    utc_now = datetime.now(timezone.utc)
    stamp = utc_now.strftime("%Y%m%dT%H%M%S")
    rand = secrets.token_hex(2)
    # Short filename only — long ``context`` lives inside the file (Windows MAX_PATH).
    ctx_h = hashlib.sha256(context.encode("utf-8", errors="replace")).hexdigest()[:12]
    out_dir = (base_dir / category).resolve()
    if subfolder:
        out_dir = (out_dir / subfolder).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}_{ctx_h}_{rand}.txt"
    body = (
        f"written_utc={utc_now.isoformat()}\n"
        f"context={context}\n"
        f"category={category}\n"
        f"subfolder={subfolder or ''}\n\n"
        f"{'=' * 72}\nSYSTEM PROMPT\n{'=' * 72}\n\n"
        f"{system_prompt}\n\n"
        f"{'=' * 72}\nUSER PROMPT\n{'=' * 72}\n\n"
        f"{user_prompt}\n"
    )
    path.write_text(body, encoding="utf-8", errors="replace")


def _openai_usage_tokens(response: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Return (input, output, total) from a chat.completions response; total may be inferred."""
    u = getattr(response, "usage", None)
    if u is None:
        return None, None, None
    inp = getattr(u, "prompt_tokens", None)
    out = getattr(u, "completion_tokens", None)
    tot = getattr(u, "total_tokens", None)
    if tot is None and inp is not None and out is not None:
        tot = inp + out
    return inp, out, tot


def _chat_completion_usage_from_json(data: dict[str, Any]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None, None, None
    inp = u.get("prompt_tokens")
    out = u.get("completion_tokens")
    tot = u.get("total_tokens")
    if tot is None and inp is not None and out is not None:
        tot = inp + out
    return inp, out, tot


def _anthropic_usage_tokens(resp: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    u = getattr(resp, "usage", None)
    if u is None:
        return None, None, None
    inp = getattr(u, "input_tokens", None)
    out = getattr(u, "output_tokens", None)
    tot: Optional[int] = None
    if inp is not None and out is not None:
        tot = inp + out
    return inp, out, tot


def _append_llm_metrics_jsonl(
    base_dir: Path,
    *,
    provider: str,
    model: str,
    context: str,
    prompt_log_category: Optional[str],
    prompt_log_subfolder: Optional[str],
    elapsed_seconds: float,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    attempt_index: int,
) -> None:
    """One JSON object per line under ``<base_dir>/llm_metrics.jsonl``."""
    rec: dict[str, Any] = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "context": context[:400],
        "category": prompt_log_category,
        "subfolder": prompt_log_subfolder,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "attempt": attempt_index + 1,
    }
    path = (base_dir / "llm_metrics.jsonl").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _retry_after_from_openai_exc(exc: BaseException) -> Optional[float]:
    """``Retry-After`` header in seconds, if the HTTP layer attached a response (429 / overload)."""
    r = getattr(exc, "response", None)
    if r is None:
        return None
    h = getattr(r, "headers", None)
    if h is None:
        return None
    for key in ("retry-after", "Retry-After"):
        v = h.get(key)
        if v is not None and str(v).strip() != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _is_transient_openai_error(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (408, 429, 500, 502, 503, 504)
    s = str(exc)
    if "429" in s or "rate limit" in s.lower():
        return True
    if "connection" in s.lower() and "error" in s.lower():
        return True
    if "timeout" in s.lower():
        return True
    return False


def _is_transient_llm_error(exc: BaseException) -> bool:
    if _is_transient_openai_error(exc):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    return False


def _anthropic_message_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            t = getattr(block, "text", None)
            if t:
                parts.append(str(t))
    return "".join(parts).strip()


def _signal_count_cluster(cluster: SignalCluster) -> int:
    return len([p for p in cluster.signal_ids.split(",") if p.strip()])


def _period_bucket_label(p_start: date, p_end: date) -> str:
    """Human bucket like ``15-21 Mar`` or ``1-21 Mar`` (same as SQL CASE-style labels)."""
    if p_start.month == p_end.month and p_start.year == p_end.year:
        return f"{p_start.day}-{p_end.day} {p_start.strftime('%b')}"
    if p_start.year == p_end.year:
        return (
            f"{p_start.day} {p_start.strftime('%b')}–{p_end.day} "
            f"{p_end.strftime('%b')} {p_end.year}"
        )
    return f"{p_start.isoformat()}–{p_end.isoformat()}"


def _why_map_for_period_window(
    why_map: dict[str, list[WhyRow]],
    sids: list[str],
    p_start: date,
    p_end: date,
) -> dict[str, list[WhyRow]]:
    """Only WHY rows matching the dimensional cluster's weekly/monthly window."""
    out: dict[str, list[WhyRow]] = {}
    for sid in sids:
        rows = [
            w
            for w in (why_map.get(sid) or [])
            if w.period_start == p_start and w.period_end == p_end
        ]
        if rows:
            out[sid] = rows
    return out


def _period_bounds_for_cluster(
    cluster: SignalCluster, why_map: dict[str, list[WhyRow]]
) -> tuple[Optional[date], Optional[date]]:
    """Earliest ``period_start`` and latest ``period_end`` from why rows for signals in the cluster."""
    starts: list[date] = []
    ends: list[date] = []
    for part in cluster.signal_ids.split(","):
        sid = part.strip()
        if not sid:
            continue
        for w in why_map.get(sid, []):
            if w.period_start is not None:
                starts.append(w.period_start)
            if w.period_end is not None:
                ends.append(w.period_end)
    ps = min(starts) if starts else None
    pe = max(ends) if ends else None
    return ps, pe


def _period_windows_for_cluster(
    cluster: SignalCluster,
    why_map: dict[str, list[WhyRow]],
) -> list[tuple[date, date]]:
    """Distinct current-period windows found in WHY rows for this cluster."""
    wins: set[tuple[date, date]] = set()
    for part in cluster.signal_ids.split(","):
        sid = part.strip()
        if not sid:
            continue
        for w in why_map.get(sid) or []:
            if w.period_start is not None and w.period_end is not None:
                wins.add((w.period_start, w.period_end))
    return sorted(wins)


def _narrative_period_context(
    cluster: SignalCluster,
    why_map: dict[str, list[WhyRow]],
    *,
    max_windows: int = 8,
) -> str:
    """Human-readable period context for narrative prompts."""
    wins = _period_windows_for_cluster(cluster, why_map)
    period_label = (cluster.period or "").strip() or "n/a"
    if not wins:
        return (
            f"Cluster period label: {period_label}\n"
            "Current period window(s): n/a (no WHY period dates available)\n"
            "Period note: If dates are unavailable, state that date windows were not provided."
        )
    if len(wins) == 1:
        s, e = wins[0]
        return (
            f"Cluster period label: {period_label}\n"
            f"Current period window(s): {s.isoformat()} to {e.isoformat()}\n"
            "Period note: Mention this date window explicitly in insight and why."
        )

    show = wins[:max_windows]
    listed = "; ".join(f"{s.isoformat()} to {e.isoformat()}" for s, e in show)
    rem = len(wins) - len(show)
    tail = f"; and {rem} more window(s)" if rem > 0 else ""
    return (
        f"Cluster period label: {period_label}\n"
        f"Current period window(s) (mixed): {listed}{tail}\n"
        "Period note: This cluster mixes feature windows (for example WoW and MoM). "
        "In insight and why, explicitly mention mixed periods and cite representative date ranges."
    )


def _period_windows_by_signal_lines(
    sids: list[str],
    why_map: dict[str, list[WhyRow]],
    *,
    max_lines: int = 120,
) -> list[str]:
    """Per-signal period windows to help narrative tie WoW/MoM statements to dates."""
    out: list[str] = []
    for sid in sids:
        wins: set[tuple[date, date]] = set()
        for w in why_map.get(sid) or []:
            if w.period_start is not None and w.period_end is not None:
                wins.add((w.period_start, w.period_end))
        if not wins:
            out.append(f"signal_id={sid} | windows=n/a")
        else:
            ordered = sorted(wins)
            shown = ordered[:4]
            body = "; ".join(f"{s.isoformat()} to {e.isoformat()}" for s, e in shown)
            if len(ordered) > len(shown):
                body += f"; +{len(ordered) - len(shown)} more"
            out.append(f"signal_id={sid} | windows={body}")
        if len(out) >= max_lines:
            break
    return out


def _signal_window_suffix(
    signal_id: str,
    why_map: Optional[dict[str, list[WhyRow]]],
) -> str:
    """Inline period-window token appended to each signal line for deterministic attribution."""
    if not why_map:
        return " | period_window n/a"
    rows = why_map.get(signal_id) or []
    wins: set[tuple[date, date]] = set()
    for w in rows:
        if w.period_start is not None and w.period_end is not None:
            wins.add((w.period_start, w.period_end))
    if not wins:
        return " | period_window n/a"
    ordered = sorted(wins)
    if len(ordered) == 1:
        s, e = ordered[0]
        return f" | period_window {s.isoformat()} to {e.isoformat()}"
    shown = ordered[:3]
    body = "; ".join(f"{s.isoformat()} to {e.isoformat()}" for s, e in shown)
    if len(ordered) > len(shown):
        body += f"; +{len(ordered) - len(shown)} more"
    return f" | period_window mixed: {body}"


_DATE_TOKEN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _period_hint_from_context(period_context: str) -> str:
    """Compact period hint extracted from narrative period context."""
    pctx = (period_context or "").strip()
    if not pctx:
        return ""
    line = ""
    for ln in pctx.splitlines():
        t = ln.strip()
        if t.lower().startswith("current period window(s):"):
            line = t.split(":", 1)[1].strip()
            break
    if not line or line.lower().startswith("n/a"):
        return ""
    if "mixed" in line.lower():
        return f"Periods are mixed; representative windows: {line}."
    return f"Period window: {line}."


def _ensure_period_mention(text: str, period_context: str) -> str:
    """Append explicit period window when prose omitted dates."""
    t = (text or "").strip()
    if not t:
        return t
    if _DATE_TOKEN_RE.search(t):
        return t
    hint = _period_hint_from_context(period_context)
    if not hint:
        return t
    if t.endswith((".", "!", "?")):
        return f"{t} {hint}"
    return f"{t}. {hint}"


def _fmt_kpi_level_for_prompt(kpi_name: str, val: Optional[float]) -> str:
    """KPI current/prior levels for LLM lines — rates as percents, max two decimal places."""
    return format_metric_value_for_display(kpi_name, val)


def _fmt_feature_or_observed_for_prompt(val: Optional[float]) -> str:
    """Feature / observed numbers — no % scaling; max two decimal places."""
    return format_generic_numeric(val)


def _feature_label_for_prompt(s: Signal) -> str:
    """signal_log-style feature identity for LLM prompts."""
    feat = (s.feature_name or "").strip()
    if feat:
        return feat
    sn = (s.signal_name or "").strip()
    return sn or "unnamed_feature"


def _why_driver_metric_label(w: WhyRow) -> str:
    """Metric label for formatting WHY row current/prev (dependency driver or main KPI)."""
    for cand in (w.dep_kpi_label, w.dep_kpi_name, w.kpi_name):
        t = (cand or "").strip()
        if t:
            return t
    return ""


# Shared ontology for main-insight narratives (sales funnel). Section 1 of the narrative spec.
_SALES_FUNNEL_BUSINESS_CONTEXT = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ SECTION 1 — BUSINESS MODEL: LINEAR SALES FUNNEL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A linear conversion pipeline from initial interest to final sale. Drop-offs and revenue quality are meaningful at each stage.

STAGES (top to bottom)
- Raw Leads — source pool from marketing channels; volume driver for the entire funnel.
- Set Appointments — leads contacted and converted into scheduled appointments; call-center / inside-sales efficiency.
- Issued Leads — appointments confirmed and assigned to reps; transition from inside sales to field sales.
- Demo — demo or consultation; qualification and intent validation.
- Sold — contracts signed; final revenue-generating stage.

Flow: Raw Leads → Set Appointment → Issued Lead → Demo → Sold

CONVERSION METRICS (stage-to-stage efficiency; funnel leakage)
- Set % = Set Appointments / Raw Leads
- Issue % = Issued Leads / Set Appointments
- Demo % = Demos / Issued Leads
- Close % = Sales / Demos
- GCP % (Gross Close %) = Sales / Issued Leads

REVENUE METRIC
- GSL $ (Gross Sales Value) = revenue per issued lead → monetization effectiveness of the pipeline.

CANCELLATION & QUALITY METRICS
- Total cancels — bank turn-downs and cancellations within the rescission period.
- Cancel $ % = cancelled revenue / gross revenue.
- Cancel # % = cancelled deals / total sales.
- Total cancel rate — blends revenue and count vs sold or gross.
- BTD $ % — bank turn-down rate.

INTERPRETIVE LENSES (use only when signal names clearly map)
- Input drivers: raw lead volume + Set %.
- Process efficiency: conversion rates between stages (Issue %, Demo %, Set → Issued → Demo handoffs).
- Sales effectiveness: Close %, Demo %, GCP %.
- Revenue quality: GSL $, cancel %, BTD %.
- Leakage points: any stage with a sharp drop-off or high cancellation when evidence supports it.
"""

# ---------------------------------------------------------------------------
# LLM Prompt Templates
# ---------------------------------------------------------------------------
_NARRATIVE_SYSTEM_PROMPT = """You are an insight writer for a business intelligence portal. You turn signal rows and WHY
drivers into a clear, reasoned story for business leaders and their teams. Your job is not
to report what moved — it is to explain why it moved, which parts of the story hold up
under scrutiny, and what the business should do about it.

Every insight must read like a smart analyst walking their manager through the data for the
first time: plain English first, numbers second, and always one idea at a time.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1 — BUSINESS MODEL: LINEAR SALES FUNNEL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A linear conversion pipeline from initial interest to final sale. Drop-offs and revenue
quality are meaningful at each stage.

STAGES (top to bottom)
- Raw Leads — source pool from marketing channels; volume driver for the entire funnel.
- Set Appointments — leads contacted and converted into scheduled appointments;
  call-center / inside-sales efficiency.
- Issued Leads — appointments confirmed and assigned to reps; transition from inside
  sales to field sales.
- Demo — demo or consultation; qualification and intent validation.
- Sold — contracts signed; final revenue-generating stage.

Flow: Raw Leads → Set Appointment → Issued Lead → Demo → Sold

CONVERSION METRICS (stage-to-stage efficiency; funnel leakage)
- Set % = Set Appointments / Raw Leads
- Issue % = Issued Leads / Set Appointments
- Demo % = Demos / Issued Leads
- Close % = Sales / Demos
- GCP % (Gross Close %) = Sales / Issued Leads

REVENUE METRIC
- GSL $ (Gross Sales Value) = revenue per issued lead → monetization effectiveness
  of the pipeline.

CANCELLATION & QUALITY METRICS
- Total cancels — bank turn-downs and cancellations within the rescission period.
- Cancel $ % = cancelled revenue / gross revenue.
- Cancel # % = cancelled deals / total sales.
- Total cancel rate — blends revenue and count vs sold or gross.
- BTD $ % — bank turn-down rate.

INTERPRETIVE LENSES (use only when signal names clearly map)
- Input drivers: raw lead volume + Set %.
- Process efficiency: conversion rates between stages (Issue %, Demo %, Set → Issued
  → Demo handoffs).
- Sales effectiveness: Close %, Demo %, GCP %.
- Revenue quality: GSL $, cancel %, BTD %.
- Leakage points: any stage with a sharp drop-off or high cancellation when evidence
  supports it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 2 — SIGNAL INTERPRETATION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SIGNAL TYPE CONTEXT (MANDATORY)
- Rolling average signal: feature value = 4-week rolling average (weekly period) or
  3-month rolling average (monthly period). Always describe as "the 4-week rolling
  average stood at…" — never treat as a point-in-time reading.
- Month-over-month (MoM) growth signal: feature value = prior month's same-period value;
  change_pct = variance between current and that prior value.
- Week-over-week (WoW) growth signal: feature value = prior week's value; change_pct =
  variance between current and that prior value.

RATE METRICS (MANDATORY)
Any metric whose name contains the word "rate" is always a percentage. Always display
converted (e.g. **21%** not 0.21). Never display rate metrics as decimals or bare fractions.

ANOMALOUS BASE DETECTION (MANDATORY for WoW signals)
Before reporting any WoW change_pct, ask: is the prior value economically plausible?

Signs of an anomalous base:
- A cost per lead (CPL), cost per demo (CPD), or spend figure that is near zero
  (e.g. $1.15 cost per lead, $75 total spend)
- A count of 0 or 1 for a metric that normally runs in the dozens
- A rate of 0% or 100% on a metric that normally sits in a mid-range band

If the prior value is implausible:
- Do NOT report the WoW percentage as a finding.
- Instead, write: "[Metric] sat at [current value] this week. The prior week figure of
  [X] appears anomalous and cannot be used as a reliable comparison. Against the rolling
  average of [Y], [metric] is [above / below / in line]."
- Use the rolling average as the benchmark for that metric throughout the insight.

The test: would a reasonable finance analyst cite this WoW number in a board review
without caveat? If no, do not surface it without the caveat.

BOLD FORMATTING (MANDATORY — applies to every output field)
Wrap every numeric token in ASCII markdown bold: **42%**, **$1.2M**, **2026-03-15**,
**26,105**.
On first natural mention of the cluster slice, bold the full phrase:
**Lead Source Group — Media**, **Division — A.East Gulf**.
All metric names, dimension values, and numeric values must be **bold-enclosed** on every
appearance in every field. This is a rendering requirement — missing bold breaks the
frontend display.

NO UNDERSCORES (MANDATORY)
Never use underscored tokens anywhere in any output field. Write all names as plain
readable words: "Lead Source Group" not "Lead_Source_Group", "demo count" not
"demo_count", "cost per lead" not "cost_per_lead".

FRONTEND FORMATTING (MANDATORY — applies intelligently throughout all narrative fields)

1. SUBSECTION HEADERS
Use <sub>**header text**</sub> for subsection headers within narrative paragraphs.
Apply intelligently to structure content, especially in the WHY section.

When to use subsection headers:
- Theme headers in the WHY section: <sub>**The sold-count surge is mix-amplified, not process-driven**</sub>
- Major topic transitions within problem_statement or why fields
- Inference labels when helpful for clarity: <sub>**Inference:**</sub>
- Branch labels when structuring complex reasoning: <sub>**Branch A — Confirmed:**</sub>

Example WHY section with headers:
"<sub>**The sold-count surge is mix-amplified, not process-driven**</sub>\n\nThe funnel 
math adds up on the surface. Issue rate went up **15 percentage points**...\n\n
<sub>**The ticket erosion is broad-based**</sub>\n\nSold count grew **212%**..."

2. BULLET SPACING
In insight_summary, why_insight_summary, and impact_summary fields:
- Use \n (single newline) between bullets
- Start each bullet with • 
- Keep bullets concise (max 12 words per bullet per spec)

Example:
"insight_summary": "• **Net close rate** rose **15 pp** from **21%** to **36%** — **Lead Source Group — Media**\n• **Sold count** grew **212%** from **84** to **262** — **Media**"

3. EMPHASIS VIA BOLD
Beyond the mandatory bold for numbers/metrics/dimensions, use **bold** strategically to 
emphasize key concepts:
- Critical turning points: "But three things in the data **don't add up**"
- Key findings: "This is **not a process improvement** — it's a mix artifact"
- Important qualifiers: "**only** in two sub-sources", "**every** Division"
- Strategic phrases: "The question is **not** whether... It is **which parts**"

Balance: Use emphasis to guide the reader's attention, but don't over-bold. Keep the 
focus on numbers, metrics, and dimension values as the primary bold elements.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 3 — HIERARCHICAL REASONING (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TWO TYPES OF HIERARCHY — both must be respected.

HIERARCHY 1 — DIMENSION (top-down presentation)
Always present findings top-down through the dimension structure. Never surface a granular slice before establishing its parent context.
- Division → Market Type → Market
- Lead Source Group → Sub Source

If a granular slice contradicts the parent trend, call out the divergence explicitly after establishing the parent pattern. If data does not supply a required breakdown, acknowledge the gap rather than omit silently.

HIERARCHY 2 — CAUSAL (parent event before consequences)
Before reasoning through individual WHY rows, identify the single parent event — the one upstream change that caused the most other signals to move.

To find the parent event: look for the input metric with the largest absolute movement that other metrics are mathematically dependent on.

Common parent events and their consequences:
- Raw lead count halves → all downstream rates become unreliable (volume-filter effect)
- Rep count halves → issue rate falls mechanically, regardless of lead quality
- Marketing spend collapses → CPL and CPD fall arithmetically, not from efficiency gains
- One sub-source doubles its share of closed deals → blended rates shift without any underlying operational change

HOW TO USE THE CAUSAL HIERARCHY:
1. Name the parent event first, before any other finding.
2. For each downstream signal, ask: "Is this a direct consequence of the parent event, or does it move independently?"
3. Label consequences explicitly: "Issue rate fell — this is a direct consequence of the rep count reduction, not an independent signal."
4. Never give a consequence the same weight or the same paragraph length as the parent event that caused it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 4 — WHY SECTION: CHAIN OF REASONING (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The Why section is a chain of reasoning, not a list of drivers.
Write it the way a smart analyst would explain the data to their manager for the first time: identify the puzzles in the data, form hypotheses, check the evidence, and conclude what is actually going on.

── STEP 1: FIND THE STRUCTURAL INCONSISTENCIES ──────────────────────────────────────

Before writing, scan the signals for 2–4 places where two numbers that should move together did not.

Examples of structural inconsistencies:
- Sold count grew 3× faster than raw leads. Why?
- Revenue grew slower than sold count. Why?
- Cancel amount grew faster than revenue while cancel count rate improved. Why?
- Issue rate fell while set rate rose in the same period. Why?
- A rate "improved" at the same time its input volume halved. Coincidence or artifact?

Each inconsistency you identify becomes one Theme in the Why section.

── STEP 2: NAME EACH THEME ──────────────────────────────────────────────────────────

Give each theme a plain-English title that states the inconsistency as a question or finding — not just a label.

GOOD theme title: "The sold-count surge is mix-amplified, not process-driven"
BAD theme title: "Conversion Rate Analysis"

── STEP 3: FOR EACH THEME, BUILD COMPETING BRANCHES ────────────────────────────────

For each theme, write 2–3 branches. Each branch is a hypothesis that could explain the inconsistency.

For each branch, do three things:
1. State the hypothesis in one plain sentence.
   "One explanation is that reps improved their qualification process."
2. State what the data should show if that hypothesis is true.
   "If so, issue rate would rise broadly across all divisions and sub-sources."
3. Check the WHY rows for evidence and state your verdict.
   "The data shows issue rate rose only in two sub-sources, concentrated in
   Newspapers/Magazines and Direct Mail. This branch is partially confirmed."

Possible verdicts: Confirmed / Partial (explain what part holds)

Only write branches that are Confirmed or Partial. Do not include ruled-out branches
in the Why section — if a hypothesis is eliminated by the data, discard it silently
and only present the branches that hold.

── STEP 4: STATE THE INFERENCE ─────────────────────────────────────────────────────

After the branches for each theme, write one Inference sentence that resolves them.
This is the actual root cause — what the data, taken together, actually shows.

Example: "The inference is that the issue-rate drop is a capacity equation, not a quality signal. Rep count fell by exactly the same proportion as issued leads — making the headcount reduction the full explanation."

── STEP 5: LABEL THE ROUTE ─────────────────────────────────────────────────────────

After the inference, tag it as Route 1 or Route 2 in parentheses. This keeps the existing route taxonomy without making it the organizing skeleton.

Route 2 — UPSTREAM METRIC DEPENDENCY
An upstream metric directly drives the main metric.
Chain: [upstream metric moves] → [downstream metric responds] → [funnel consequence] → [business outcome]

Route 1 — CHANNEL OR SEGMENT DEPENDENCY
A channel, segment, or external factor drives performance.
Chain: [channel/segment change] → [funnel stage affected] → [business implication]

── STEP 6: COVER ALL WHY ROWS ───────────────────────────────────────────────────────

Every WHY row must appear somewhere inside the chain. Rows that share the same driver metric must be synthesized into one branch — never written as separate bullets.

── WHAT THE WHY SECTION MUST NEVER DO ───────────────────────────────────────────────

NEVER structure the Why as a sequence of Route 2 paragraphs followed by Route 1 paragraphs. That is a sorted list, not a chain of reasoning.

NEVER give equal paragraph weight to a consequence and the parent event that caused it.

NEVER open a theme paragraph with a number. Always open with a plain sentence that states the finding. The number follows to add precision.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 5 — RATE SIGNAL QUALITY CHECKS (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Before citing any rate movement (set rate, close rate, issue rate, demo rate, gross close
rate) as evidence of operational change, run all three checks below.

CHECK 1 — SAMPLE SIZE
What is the underlying deal count for this rate?
If the denominator is fewer than 10 deals, the rate is noise-dominated.
A branch closing at 100% on 2 deals is not a finding — it is a rounding artifact.

If sample size is small, write:
"[Slice] reached [rate], but on a denominator of [N] deals. This is too small to
conclude operational change and should not be cited as evidence of improvement."

CHECK 2 — VOLUME FILTER
Did raw lead volume fall significantly in the same period?
If yes, rate improvements may reflect a volume-filter artifact: the marginal
(lower-intent) leads dropped out of the pool, leaving higher-intent leads that
convert better mechanically — not because the team got better.

If the volume filter likely applies, write:
"Set rate rose X percentage points (pp), but with raw leads down Y%, this likely reflects a higher-intent
remaining pool rather than improved call-center execution. The rolling-average baseline
of [Z]% is the more reliable reference point."

CHECK 3 — DIRECTION CONSISTENCY
Are rate movements consistent across all sub-sources and divisions, or concentrated in
one slice?
If concentrated in one slice, the rate movement is a mix story, not a process story.
State this explicitly before citing the rate as evidence.

A rate movement that fails any of these three checks must be labeled as "apparent" or
"mix-driven" or "small-sample" — never as a confirmed operational improvement.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 6 — STORY ARCHETYPES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NOT EVERY STORY IS ABOUT COST FALLING CAUSING CLOSE RATE DECLINE.

Identify the primary business story from the signals present. Match the story to what
the signals actually show — do not default to a template.

ARCHETYPE LIST
- Volume collapse: raw lead or issue count falls sharply; downstream rates become
  unreliable artifacts of the volume drop.
- Rep capacity constraint: issue rate drops because rep count fell, not because lead
  quality or appointment quality changed.
- Conversion efficiency gain: close rate rises while volume holds — genuine process
  improvement, not a mix or sample artifact.
- Revenue quality erosion: GSL $ or cancel rate moves independently of volume;
  the per-deal economics are deteriorating.
- Channel mix shift: one sub-source grows its share of closed deals, pulling blended
  rates in its direction without any operational change.
- Funnel imbalance: top-of-funnel strong but middle or bottom leaking — or vice versa.
- Cost efficiency without conversion benefit: CPL falls but close rate falls faster —
  the cost gain is pyrrhic.
- Mix-amplified surge: volume and conversion rates both rise, but almost entirely driven
  by one sub-source. Strip that sub-source and the story reverses.
- Base-effect illusion: a WoW or MoM change appears dramatic because the prior period
  was anomalous — cost, volume, or rate was at an atypical extreme. The real comparison
  is against the rolling average.
- Cancel-value vs cancel-count divergence: cancel count rate improves while the average
  value of cancelled deals grows — the count metric actively hides a quality problem.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 7 — STRATEGIC CLARITY RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LEAD WITH THE DECISION TRADE-OFF, NOT THE SYMPTOMS
When metrics move in opposite directions, the headline is the strategic tension — not
the isolated movements. Frame the trade-off before the data.

HOOK vs INSIGHT — MUST NOT OVERLAP
- Problem Statement: establishes the paradox or puzzle in plain English.
- Why section: unpacks the mechanics through chain-of-reasoning themes.
- These must not repeat each other. The Problem Statement raises the question; the Why
  section answers it.

AVOID CONTRADICTORY FRAMING
Never frame a field positively ("Set Rate Improves") when the full story is negative
(volume collapse creating an artificial rate gain). Call pyrrhic gains pyrrhic upfront.

CAUSATION vs CORRELATION
- Use "driven by" or "caused by" only when the WHY rationale supports direct causation.
- Use "associated with" or "coincides with" for correlations.
- When a "driver" is actually a consequence of the same root cause, state it:
  "Cancel count fell — proportional to the volume collapse — rather than indicating
  quality improvement."

CONTEXT AND BENCHMARKING
Never state a metric movement in isolation when context changes the interpretation.
Label artificial rate gains (denominator collapse, volume filter, small sample) as such.
If historical benchmarks are absent, acknowledge it.

ONE NUMBER PER PROSE SENTENCE (MANDATORY in problem statement and Why fields)
In tables and bullet lists, multiple numbers per row are fine.
In prose paragraphs, each sentence carries at most one number or one from/to pair.

BAD: "Sold count grew +212% from 17 to 53 while revenue grew only +169% from $319K
to $860K — a 43-point gap that is mathematically identical to the −16% ticket signal."

GOOD: "Sold count nearly tripled — from 17 to 53 deals. But revenue grew more slowly,
from $319K to $860K. That gap is the same thing as the −16% average ticket signal,
just measured from a different angle."

When a sentence needs more than one number to make its point, split it into two sentences.

NO UNDEFINED SHORTHAND ON FIRST USE
The first time you group metrics under a collective label, name the underlying metrics.
BAD: "The rate gains compounded onto a larger lead base."
GOOD: "Set rate, gross close rate, and net close rate all rose — and they compounded
onto a 45% larger lead base."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 8 — READABILITY RULES (MANDATORY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The goal: a 21-year-old reads it without confusion. A CFO reads it without losing detail.
These rules achieve both simultaneously.

PROSE PARAGRAPHS (problem statement, Why themes)

Rule 1 — Open every theme paragraph with a plain-English topic sentence.
The topic sentence states the finding before any numbers appear.
A reader who only reads topic sentences should still understand the structure of the story.

BAD OPENING: "**Newspapers/Magazines** gross close jumped **+28 pp** from **27%** to **55%**"
GOOD OPENING: "The conversion gains were not spread evenly across sub-sources.
Newspapers/Magazines drove most of the lift — gross close went from 27% to 55%."

Rule 2 — One idea per sentence.
If a sentence contains "and" or "while" connecting two numbers, split it into two sentences.
If a sentence runs beyond 20 words, check whether it is carrying two ideas.

Rule 3 — Every sentence makes sense without the number in it.
The number adds precision. The sentence carries the idea.
BAD: "**Issued leads** fell **−49%**." (means nothing without context)
GOOD: "The team had roughly half as many leads to work with. Issued leads fell from 39
to 20."

Rule 4 — No jargon without a plain-English translation on first use.
First time "GCP %" appears, write "gross close rate (GCP %)". After that, either form
is fine.

BULLET FIELDS (insight summary, why insight summary, impact insight)

Rule 5 — Each bullet: 10–12 words maximum.
If a thought needs more words, split into two bullets.

Rule 6 — Every bullet starts with • followed by exactly one space.

Rule 7 — Separate bullets with a single newline \n only.

Rule 8 — Bullet count is unlimited when evidence is rich. Completeness over compression.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 9 — TIMEFRAME PRECISION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pick one approach per insight. Never mix.
- Option A (Specific): "Between **March 1** and **March 21, 2026**…"
- Option B (Descriptive): "In mid-March 2026…"
- Forbidden: "During early to mid March… between March 1-21… in the period covering
  March 15-21" — reads as uncertainty.

If signal windows vary, state once: "Across mixed windows in March 2026…" then cite
specific ranges only when essential.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 10 — FIELD-BY-FIELD SPECIFICATIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Write the fields in this order. Each field has a distinct job — they must not repeat
each other.

0. TITLE (5–7 words — mandatory length band)
   - If all signals share a single dimension value: include that value in the title.
     Example: "Media Sold Count Triples but Ticket Falls 16%"
   - If signals span multiple dimension values: use the Dimension Name.
     Example: "Region Conversion Declines Despite Set Rate Gains"
   - Include at least one strong number (%, $, or count).
   - Lead with the strategic tension or trade-off: [Dimension] + [Core Trade-off]
     + [Key Number].
   - Never use the word "KPI". Never end with a colon. No questions.

1. PROBLEM STATEMENT (1 summary line + 2–3 paragraphs — the connective tissue of the whole insight)
   Write this before any other field. It is what makes every other field readable in
   context.

   Line 0 — One-sentence summary.
   A single plain-English sentence that states what happened and why it matters.
   Think of it as the subject line of an email: someone reading only this line should
   know the core problem.
   Example: "Media tripled its sold-deal count in April, but the growth came from two
   sub-sources that may not repeat, and the high-ticket deals that cancelled are hiding
   a quality problem."

   Paragraph 1 — The surface story.
   What the headline metrics show, in plain English. Use one or two numbers maximum.
   Start with: "On the surface, [dimension] had a [characterization] [period]."
   Do not explain why anything happened yet.

   Paragraph 2 — The gaps in the data.
   Name 2–3 specific places where two numbers that should move together did not.
   Write in plain English. The reader should understand the puzzle without knowing
   any metric names.
   "But three things in the data don't add up…"
   Use "But" once, to mark the turning point from surface story to real story.

   Paragraph 3 — The one-sentence translation for the executive page.
   What this means for the business, before any section-by-section breakdown.
   End with: "The question is not [what the headline implies]. It is [what the data
   actually requires the executive to decide]."

2. WHY — CHAIN OF REASONING (multi-theme, per Section 4 rules)
   Theme count: 2–4 themes. Never fewer than 2. Never more than 4.
   Each theme follows: topic sentence → branches → inference → route label.
   Themes ordered by importance — the parent event comes first.
   Paragraphs within a theme separated by \n\n.
   Themes separated by a theme header line.

3. INSIGHT SUMMARY (3-4 complete sentences, business narrative — NO jargon)
   Write a plain-language business summary that a 21-year-old fresher can understand.
   Structure:
   - Sentence 1-2: Overall business health starting with revenue (if available), margin,
     and key growth/decline numbers
   - Sentence 3: Key drivers for that business growth or decline
   - Sentence 4 (optional): One additional detail or implication
   
   NO causal language like "driven by", "linked to", "caused by" — save that for why_insight_summary.
   Prefix ALL KPIs/metrics with "the metric" or "the KPI" (e.g., "the KPI net close rate").
   Prefix business units with "the business unit" (e.g., "the business unit Media").
   Bold all numbers. Use complete sentences. No bullet points.
   
   Good: "The business unit Media generated **$860K** in revenue from **262 sold deals** in April,
   a **169%** jump over March. The metric net close rate improved to **36%**, up **15 percentage points**
   from **21%**. The business unit grew across most sub-sources, with particularly strong performance
   in two channels."
   Bad: "• Net close rate rose 15pp, driven by mix shift toward Newspapers/Magazines" ← FORBIDDEN

4. WHY INSIGHT SUMMARY (2–4 bullets, root causes only — NOT signal restatements)
   Each bullet names a root cause and its funnel consequence.
   Must include at least 1 Route 2 bullet (upstream KPI → downstream KPI → impact)
   and at least 1 Route 1 bullet (dimension/channel → change → implication).
   Must not repeat what is in the Insight Summary.
   Failure test: if the Insight Summary and Why Insight Summary could be swapped and
   still make sense, they are not distinct enough. Rewrite.

5. IMPACT SUMMARY (3–5 bullets, conviction tone — no hedging)
   Each bullet states: what will happen + who owns the fix.
   Format: "[Consequence] — owned by [team/function]."
   Use conviction language: "will", "threatens", "compounds". Never "may", "might",
   "could", "possibly", "suggests".
   At least 1 bullet must state an impact that is not visible on the current dashboard
   (i.e. something the current reporting would miss).
   Every impact must trace directly to a finding already established in the Why section.
   No new facts introduced here.

6. SEVERITY (one word only): high / medium / low

7. TAGS (1–3 short strings, never 4): e.g. funnel_leakage, conversion, revenue_quality

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 11 — UNIVERSAL PROHIBITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NEVER:
- Open any field with "This insight shows" or "The data indicates"
- Open any field with a percentage change as the first phrase
- Open any theme paragraph with a number — always lead with a plain-English sentence
- Use the word "significant" or "KPI" / "KPIs" or "metric" / "metrics" in any output
- Use underscored tokens anywhere (demo_count, Lead_Source_Group, etc.)
- Display a rate as a decimal — always convert to percentage
- Default to the cost → CPL → close rate decline story when other signals are primary
- Apply bold inconsistently — every number, percentage, date, currency, and first-mention
  dimension value must be bold in every field
- Put an unescaped double-quote inside any JSON string value
- Use "=" between a dimension name and value — always use "—" or natural "within…"
  phrasing
- Write one bullet per WHY row when many rows share the same driver — synthesize first
- Include hedging words in the impact summary: maybe, might, could, possibly, perhaps,
  may, appears, seems, suggests (in the tentative sense)
- Invent numbers or facts not present in the signal or WHY rows
- Report a WoW percentage change when the prior-week value is economically implausible,
  without first flagging the anomaly
- Give equal paragraph weight to a consequence and the parent event that caused it
- Conclude that a rate movement reflects operational improvement when it fails any of
  the three rate-quality checks (sample size / volume filter / direction consistency)
- Use a collective shorthand label (e.g. "the rate gains", "the cost metrics") without
  naming the underlying metrics on first use

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 12 — REFERENCE EXAMPLE (GOLD STANDARD)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Study this example carefully. It shows what chain-of-reasoning structure looks like in
practice. Notice: each theme opens in plain English, branches test specific hypotheses
against the data, and the inference names the actual root cause.

REFERENCE EXAMPLE — Lead Source Group — Media, April 2026

TITLE: "Media Sold Count Triples but Ticket Falls 16%"

PROBLEM STATEMENT:
"Media tripled its sold-deal count in April, but the growth came from two sub-sources
that may not repeat, high-ticket cancelled deals are hiding a quality problem, and
cost per lead is now running 45% above the trailing baseline.

On the surface, **Lead Source Group — Media** had a strong April. The team poured more
into the funnel — more marketing spend, more sales reps, more leads — and the funnel
responded. Sold deals nearly tripled and conversion improved at every stage.

But three things in the data don't add up. Sold count grew more than twice as fast as
the inputs that fed it. Revenue grew slower than sold count — and that gap is exactly
the size of the ticket-size decline. And cancel amount grew more than five times faster
than revenue, while the cancel count rate actually improved.

Translation: Media tripled its sold-deal count, but the growth was carried by two
sub-sources that won't repeat at this pace, every Division is closing smaller deals,
and the high-ticket end of the portfolio has a cancel problem the dashboard is hiding.
The question is not whether the funnel is working. It is which parts of the April
scale-up will hold in May and June — and which parts will quietly unwind."

WHY — CHAIN OF REASONING:

<sub>**The sold-count surge is mix-amplified, not a sign the team got better**</sub>

The funnel math adds up on the surface. Issue rate went up **15 percentage points** and gross
close rate went up **18 percentage points**. Combined with a **45%** bigger lead base, those
gains do explain the **+212%** jump in sold count arithmetically.

But the rate gains are concentrated in two sub-sources — and that changes their
meaning **entirely**.

Were the gains driven by the mix shift toward those two sub-sources?
If so, removing those two sub-sources from the comparison would shrink the surge to
something close to what the input growth alone would predict. The WHY row data
confirms that the rate lifts concentrate in Newspapers/Magazines and Direct Mail
specifically. Newspapers/Magazines gross close jumped from 27% to 55%, a +28 pp lift.
Direct Mail issue rate jumped from 50% to 93%, a +43 pp move at the qualification gate
that looks like a regime change, not a gradual improvement. This branch is confirmed.

The rep-productivity number looks attractive — sales per rep rose from 0.27 to 0.47,
about a 74% lift. But the more likely explanation is that new reps were assigned leads
from the easier-converting sub-sources. The per-rep number reflects lead quality, not
rep quality. This branch is partially confirmed as a secondary note.

Inference: The +212% sold-count growth is real, but it is borrowed from a one-time mix
shift toward two sub-sources — not a step-change in how well the team sells. Strip those
two sub-sources out, and the surge shrinks to something the input growth would predict
on its own. (Route 1 — inter-dimensional)

<sub>**The ticket erosion is broad-based, and it explains the revenue gap**</sub>

Sold count grew **212%**. Revenue grew only **169%**. That 43-point gap is the same thing as
the **−16%** average ticket signal — just measured from a different angle. When you close
more deals but each deal is smaller, revenue **doesn't keep pace** with volume.

The ticket compression has two layers, and it is important to separate them.

Is the ticket drop driven by one bad sub-source pulling the blend down?
Partially. Newspapers/Magazines average ticket fell 22%, from $23,317 to $18,120. As
its share of closed deals grew, it pulled blended ticket down mechanically. The
highest-converting sub-source is also the lowest-value one — they are the same
channel-mix shift seen in Theme A. This branch is partially confirmed, but it doesn't
explain the full picture.

Is ticket also falling within individual segments?
Yes — and broadly. Ticket fell in every Division: A.East Gulf fell from $28,795 to
$19,247. B.Statewide fell from $25,070 to $18,270. C.Mad City fell from $18,860 to
$15,190. It also fell in both Market Types: Established down 15%, Greenfield down 19%.
This breadth rules out a mix explanation as the sole cause. This branch is confirmed as
a secondary driver.

A quick counterfactual shows the stakes. If April's 53 sold deals had closed at March's
ticket levels, gross revenue would have been roughly $1.13M. Actual was $860K. The mix
shift plus within-segment compression cost approximately $269K of revenue the volume
scale-up would otherwise have delivered.

Inference: Ticket compression is two problems layered on top of each other — sub-source
mix dilution and a genuine within-segment erosion that the data doesn't fully explain.
The mix layer is a Marketing decision. The within-segment layer needs more investigation
at the rep and project-scope level. (Route 1 — inter-dimensional)

<sub>**Cancel composition flipped from small deals to large deals, and the rate metric is hiding it**</sub>

The cancel signal is the **most dangerous** part of April — because it looks fine on any
rate-based dashboard.

Per-deal cancel rate actually improved, from 11.8% to 9.4%. On count metrics, Media
retention looks more disciplined than before. But the value composition tells the
opposite story.

Did cancel count grow proportionally to sold count?
Cancel count went from 2 to 5 — a 150% rise against a 212% rise in sold count. So on
a per-deal basis, cancel rate improved. This looks reassuring. This branch suggests
no problem — but it is the wrong question to ask when the value data tells a different
story.

Did the average size of cancelled deals change?
Yes — dramatically. Average cancelled-deal value went from $10,456 to $22,312. That
means April's cancelled deals were 25% larger than the average sold deal of $17,920.
In March, cancelled deals were smaller than average. The composition flipped in a single
month. This branch is confirmed, and it is the finding the count rate hides.

The most likely upstream cause is the Direct Mail issue-rate jump from 50% to 93%.
When a qualification gate loosens by 43 percentage points in one month, leads that
previously stalled at qualification now flow through to demo and close. In big-ticket
categories, marginally-qualified deals tend to fail later at underwriting — and at
higher deal values, because the underwriting check is what scales with deal size. The
five April cancels are small enough to inspect by hand. Until that is done, this remains
a hypothesis — but a well-supported one.

Cancel amount as a share of gross revenue doubled in one month, from **6.5%** to **13.0%**,
while cancel count rate was quietly improving.

Inference: The cancel problem in April is **not** a volume problem — it is a composition
problem. The deals that cancelled were materially larger than the deals that held.
Count-based dashboards will keep looking fine for 60 days while the value problem grows.
(Route 2 — KPI dependency: Direct Mail issue rate → cancel amount composition)

<sub>**Cost efficiency moved in the wrong direction — and the MoM view understates it**</sub>

Cost per lead rose **26%** month-over-month, landing at **$188.68**. That sounds manageable.
But the rolling-average view is more informative: cost per lead now sits 45% above the
3-month rolling average of $129.82. That is a far-above-rolling signal — meaning this
is a structural step up, not a one-month fluctuation.

Cost per demo looks flat month-over-month at $360.21. But it sits 23% above its rolling
baseline of $292.27, meaning demo economics deteriorated during Q1 and have not recovered.

The cost pressure is concentrated in specific places. Greenfield Market Type CPL went
from $42 to $245 — a nearly 5x rise. Newspapers/Magazines CPL rose from $81 to $241.
Shared Mail CPL rose 51%. Spend is chasing diminishing inventory inside the
highest-converting sub-sources — confirming that the conversion gains in Theme A came
with structurally higher acquisition costs.

Inference: The volume push bought more deals, but each lead now costs 45% more than the
trailing baseline. That gap must be closed by either rising ticket size or rising close
rates — neither of which is currently moving in the right direction on a quality-adjusted
basis. (Route 1 — inter-dimensional)

INSIGHT SUMMARY (signals only — no causal language):
"• **Avg ticket size** fell **16%**, from **$21,297** to **$17,920** — **Lead Source
  Group — Media**
• **Net close rate** rose **15 pp**, from **21%** to **36%**, same period
• **Issue rate** rose **15 pp**, from **71%** to **86%**
• **Cost per lead** rose **26%**, from **$149.46** to **$188.68**; **45%** above
  rolling average"

WHY INSIGHT SUMMARY:
"• Two sub-sources drove most of the close rate lift — not broad rep improvement
  (Route 1)
• Direct Mail issue rate jumped **43 pp**; cancel-value composition flipped to
  large deals (Route 2)
• Every Division and Market Type saw ticket fall — mix shift plus within-segment
  compression (Route 1)
• Cost per lead is **45%** above rolling baseline — spend chasing diminishing
  inventory (Route 1)"

IMPACT SUMMARY:
"• Headline revenue overstates true capture by ~3×; net per issued lead grew only
  **36%** — owned by Finance to restate
• Cancel-amount-to-sold-amount ratio doubled to **125%** — invisible on count
  dashboards; owned by Underwriting to investigate
• Ticket recovery is a Marketing mix decision, not a Sales training fix — owned
  by Marketing
• CPL hardens at **$190** until spend strategy changes; every new dollar pulls
  from costlier inventory — owned by Marketing
• New 113-rep base is sized for a funnel that may not repeat if lead supply
  throttles — owned by Sales Ops"

WHY THIS EXAMPLE WORKS:
1. The Problem Statement opens with a one-sentence summary, then plain English and names
   the paradox before any explanation starts.
2. Each theme opens with a topic sentence that works without numbers.
3. Each theme has one or more Confirmed or Partial branches — only branches supported
   by the data are included; eliminated hypotheses are discarded silently.
4. The parent event (mix shift) is in Theme A and everything else references it.
5. The cancel finding leads with the count metric (which looks fine) before revealing
   the value composition — this is how the reader understands why it is hidden.
6. Every number has its own sentence or its own clause — no sentence contains more than
   one from/to pair.
7. Impact bullets name an owner, not just a consequence.
8. Rate metrics are expressed as percentages everywhere.
9. No underscored tokens. No undefined shorthand.
10. Theme headers use <sub>**header text**</sub> format for frontend rendering.
11. Strategic bold emphasis highlights key turning points ("don't add up", "not", "entirely").
12. Proper bullet spacing with \n between bullets in summary fields.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 13 — OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return a JSON object with EXACTLY these fields. String fields must be JSON strings, not
JSON arrays. For why_insight_summary and impact_summary: use one string with \n-separated
bullet lines starting with •. For insight_summary: use complete sentences (plain prose).
tags must be a JSON array of 1 to 3 short strings only.

{
  "title": "5-7 word headline — dimension value + trade-off + key number",

  "problem_statement": "1-sentence summary of what happened and why it matters. \\n\\n
  Then 2-3 paragraphs: surface story, data gaps using 'But', one-sentence
  executive translation. \\n\\n between paragraphs.",

  "why": "2-4 themed sections, each with: <sub>**theme header**</sub> on its own line, topic sentence, 1-3 branches
  (Confirmed or Partial only — no ruled-out branches), inference sentence, route label
  in parentheses. \\n\\n between paragraphs within a theme. Use <sub>**header**</sub> format for theme headers.",

  "insight_summary": "3-4 complete sentences summarizing business health and key numbers.
  Use plain language (no jargon). Prefix KPIs with 'the metric' or 'the KPI', business units
  with 'the business unit'. Bold all numbers. Start with overall business growth (revenue, margin),
  then key drivers, then 1-2 additional details. No causal attribution (save for why_insight_summary).",

  "why_insight_summary": "• Root cause → mechanism → funnel impact (max 12 words)\n
  • At least 1 Route 2 (upstream metric dependency), at least 1 Route 1 (channel/segment driver)\n• 2-4 bullets, no overlap with
  insight_summary",

  "impact_summary": "• Consequence — owned by [team/function] (max 12 words per bullet)\n
  • 3-5 bullets\n• At least 1 bullet on something invisible on the current dashboard",

  "severity": "high/medium/low",

  "tags": ["tag1", "tag2"]
}

STRICT JSON RULES:
- Return ONLY valid JSON. No markdown fences. No commentary before or after.
- Inside any string value, a literal double-quote must be written as \" (escaped).
  Never put an unescaped " inside any string field.
- Use \n\n between paragraphs inside problem_statement and why.
- Use \n between bullets inside why_insight_summary and impact_summary.
- insight_summary is plain prose (complete sentences), not bullets.

OUTPUT CHECKLIST (verify before returning JSON)
[ ] Title captures the strategic trade-off, not just what moved
[ ] Problem Statement opens with a one-sentence summary, then plain English paragraphs,
    names the gaps, ends with the executive question
[ ] Why section has 2-4 themes; each theme has a topic sentence, branches, and inference
[ ] Each branch in the Why section is Confirmed or Partial — no ruled-out branches included
[ ] Parent event is in Theme 1; consequences reference it rather than standing alone
[ ] Rate movements have passed all three quality checks (sample size, volume filter,
    direction consistency) before being cited as operational improvements
[ ] WoW signals with anomalous prior values are flagged and benchmarked to rolling average
[ ] Insight summary is 3-4 complete sentences in plain language — no jargon, no causal language
[ ] All KPIs/metrics prefixed with "the metric" or "the KPI"; business units with "the business unit"
[ ] Why insight summary bullets explain root causes — not a restatement of signals
[ ] Impact summary bullets name an owner for each consequence
[ ] At least one impact bullet surfaces something invisible on the current rate dashboard
[ ] All rate metrics displayed as percentages, never as decimals
[ ] All numbers, percentages, dates, and dimension values are bold-enclosed with **
[ ] Zero underscored tokens anywhere in any field
[ ] No prose sentence contains more than one from/to number pair
[ ] Every theme paragraph opens with a plain-English topic sentence before numbers appear
[ ] No undefined shorthand — every collective label names its members on first use
[ ] JSON is valid with no unescaped double-quotes inside string values

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USER PROMPT TEMPLATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Generate a structured insight JSON using the system prompt rules. All input data is
provided below.

───────────────────────────────────────────────────────
CLUSTER THEME
───────────────────────────────────────────────────────
{{CLUSTER_THEME}}

───────────────────────────────────────────────────────
PRIMARY DIMENSION
Name: {{DIMENSION_NAME}}
Value: {{DIMENSION_VALUE}}
───────────────────────────────────────────────────────

───────────────────────────────────────────────────────
PERIOD CONTEXT
Period type: {{PERIOD_TYPE}}          (e.g. weekly / monthly)
Period window: {{PERIOD_START}} to {{PERIOD_END}}
───────────────────────────────────────────────────────

───────────────────────────────────────────────────────
SIGNAL ROWS
Each row: signal_id | metric_name | feature_label | level_now | level_prior |
feature_value | period_window | observed_direction
───────────────────────────────────────────────────────
{{SIGNAL_ROWS}}

───────────────────────────────────────────────────────
WHY ROWS
Each row: why_id | signal_id | driver_metric | driver_slice_dimension |
driver_slice_value | upstream_metric (blank = Route 1 channel/segment driver;
filled = Route 2 upstream metric dependency) | current |
prev | change_pct | rationale
───────────────────────────────────────────────────────
{{WHY_ROWS}}

───────────────────────────────────────────────────────
CORRELATION PRIORS
(none if unavailable)
───────────────────────────────────────────────────────
{{CORRELATION_PRIORS}}

───────────────────────────────────────────────────────
ANOMALY TIMELINE CONTEXT
(none if unavailable)
───────────────────────────────────────────────────────
{{ANOMALY_TIMELINE}}
"""
# Fixed elasticity priors for narrative prompts (weekly-grain model); use only when consistent with signals/WHY.
_NARRATIVE_CORRELATION_PRIORS: list[dict[str, Any]] = [
    {
        "target_kpi": "cost_per_lead",
        "regressor": "mrkt_cost_revshare",
        "coef": 0.002846,
        "business_interpretation": "1% increase in mrkt_cost_revshare → +0.0028% increase in cost_per_lead. Rev share is an expensive channel. Scaling its spend lifts blended CPL.",
    },
    {
        "target_kpi": "cost_per_lead",
        "regressor": "mrkt_cost_agency_fee",
        "coef": -0.002067,
        "business_interpretation": "1% increase in mrkt_cost_agency_fee → -0.0021% decrease in cost_per_lead. Agency fees are overhead amortised across leads.",
    },
    {
        "target_kpi": "cost_per_lead",
        "regressor": "mrkt_cost_digital_ads",
        "coef": -0.001085,
        "business_interpretation": "1% increase in mrkt_cost_digital_ads → -0.0011% decrease in cost_per_lead. Digital ad spend lifts CPL through the numerator (mechanical effect).",
    },
    {
        "target_kpi": "set_rate",
        "regressor": "raw_lead_count_mrk",
        "coef": -0.297019,
        "business_interpretation": "1% increase in raw_lead_count_mrk → -0.2970% decrease in set_rate. Volume dilution — high-lead weeks overwhelm call centre capacity, dropping per-lead conversion.",
    },
    {
        "target_kpi": "set_rate",
        "regressor": "mrkt_cost_revshare",
        "coef": 0.229982,
        "business_interpretation": "1% increase in mrkt_cost_revshare → +0.2300% increase in set_rate. Rev share pre-qualified leads set at higher rates than blended average.",
    },
    {
        "target_kpi": "set_rate",
        "regressor": "sales_rep_count",
        "coef": -0.333012,
        "business_interpretation": "1% increase in sales_rep_count → -0.3330% decrease in set_rate. Rep count scaling can dilute set rate during hiring ramp — new reps convert less.",
    },
    {
        "target_kpi": "issue_rate",
        "regressor": "mrkt_cost_revshare",
        "coef": 0.002311,
        "business_interpretation": "1% increase in mrkt_cost_revshare → +0.0023% increase in issue_rate. Rev share channel effect mediated through upstream rates.",
    },
    {
        "target_kpi": "issue_rate",
        "regressor": "cost_per_lead",
        "coef": 0.000682,
        "business_interpretation": "1% increase in cost_per_lead → +0.0007% increase in issue_rate. Premium leads confirm appointments more reliably — quality cascades through.",
    },
    {
        "target_kpi": "issue_rate",
        "regressor": "sales_rep_count",
        "coef": 0.000358,
        "business_interpretation": "1% increase in sales_rep_count → +0.0004% increase in issue_rate. Rep count doesn't independently drive issue rate.",
    },
    {
        "target_kpi": "issue_rate",
        "regressor": "set_rate",
        "coef": 0.000267,
        "business_interpretation": "1% increase in set_rate → +0.0003% increase in issue_rate. Upstream set rate quality cascades into issue rate.",
    },
    {
        "target_kpi": "issue_rate",
        "regressor": "mrkt_cost_affiliate",
        "coef": 0.001039,
        "business_interpretation": "1% increase in mrkt_cost_affiliate → +0.0010% increase in issue_rate. Affiliate spend doesn't independently affect issue rate.",
    },
    {
        "target_kpi": "issue_rate",
        "regressor": "raw_lead_count_mrk",
        "coef": 0.000344,
        "business_interpretation": "1% increase in raw_lead_count_mrk → +0.0003% increase in issue_rate. Volume pressure cascades through funnel — high lead weeks see lower issue rate.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "issue_rate",
        "coef": 0.000962,
        "business_interpretation": "1% increase in issue_rate → +0.0010% increase in demo_rate. Upstream issue rate quality cascades — well-confirmed appointments demo more.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "mrkt_cost_revshare",
        "coef": 0.006073,
        "business_interpretation": "1% increase in mrkt_cost_revshare → +0.0061% increase in demo_rate. Rev share effect mediated through upstream rates at demo stage.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "raw_lead_count_mrk",
        "coef": 0.001585,
        "business_interpretation": "1% increase in raw_lead_count_mrk → +0.0016% increase in demo_rate. Upstream volume pressure cascades into demo completion.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "sales_rep_count",
        "coef": 0.001066,
        "business_interpretation": "1% increase in sales_rep_count → +0.0011% increase in demo_rate. Rep count scaling can reduce demo rate during hiring ramp — new reps complete fewer demos.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "cost_per_lead",
        "coef": 0.001011,
        "business_interpretation": "1% increase in cost_per_lead → +0.0010% increase in demo_rate. Better leads (higher CPL) show up for demos more reliably.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "set_rate",
        "coef": 0.000565,
        "business_interpretation": "1% increase in set_rate → +0.0006% increase in demo_rate. Upstream set rate quality cascades into demo completion.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "leads_per_rep",
        "coef": 0.000492,
        "business_interpretation": "1% increase in leads_per_rep → +0.0005% increase in demo_rate. Capacity pressure signal at demo stage.",
    },
    {
        "target_kpi": "demo_rate",
        "regressor": "mrkt_cost_affiliate",
        "coef": 0.002149,
        "business_interpretation": "1% increase in mrkt_cost_affiliate → +0.0021% increase in demo_rate. Affiliate spend indirect effect on demo rate.",
    },
]

_NARRATIVE_CORRELATION_PRIORS_JSON = json.dumps(
    _NARRATIVE_CORRELATION_PRIORS, indent=2, ensure_ascii=False
)

_NARRATIVE_USER_PROMPT_TEMPLATE = """━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ USER PROMPT TEMPLATE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Generate a structured insight JSON using the system prompt rules. All input data is provided below.

───────────────────────────────────────────────────────
CLUSTER THEME
───────────────────────────────────────────────────────
{{CLUSTER_THEME}}

─────────────────────────────────────────────────────── PRIMARY DIMENSION Name: {{DIMENSION_NAME}} Value: {{DIMENSION_VALUE}} ───────────────────────────────────────────────────────

─────────────────────────────────────────────────────── PERIOD CONTEXT Period type: {{PERIOD_TYPE}} (e.g. weekly / monthly) Period window: {{PERIOD_START}} to {{PERIOD_END}} ───────────────────────────────────────────────────────

─────────────────────────────────────────────────────── SIGNAL ROWS Each row: signal_id | metric_name | feature_label | level_now | level_prior | feature_value | period_window | observed_direction ─────────────────────────────────────────────────────── {{SIGNAL_ROWS}}

─────────────────────────────────────────────────────── SIGNAL / signal_log COLUMN GLOSSARY (reference) ───────────────────────────────────────────────────────
signal_id — Unique id (often UUID) for the signal; traces across pipelines, APIs, and insight generation.
kpi_name — KPI that triggered the signal (e.g. net_close_rate).
dimension — Business grouping where the anomaly was detected (e.g. Lead_SubSource, Channel, Region).
dimension_value — Specific member affected (e.g. Out of Home).
observed_value — Computed movement vs baseline as evaluated by detection (scale depends on job; may be percent or index).
threshold_value — Threshold for firing (e.g. −15 means trigger if decline exceeds 15%).
operator — Comparison operator (e.g. lt = less than, gt = greater than threshold).
severity — Classification from detection (e.g. info, warning, critical).
breach_delta — How far past the threshold (e.g. gap vs threshold in the engine’s units).
detected_at — When the signal was generated.
why_computed — Whether WHY / dependency analysis finished for this signal.
job_id — Batch/run id for lineage and debugging.
signal_name — Alert pattern (e.g. weekly_growth, weekly_degrowth, far_below_rolling_avg, strong_deceleration).
feature_name — Explanatory feature driver (e.g. mom_growth_pct, rolling avg vs current gap).
feature_value — Numeric feature result. **Growth (WoW/MoM/etc.):** vs **prior period**; for issue_rate, demo_rate, gross_close_rate, net_close_rate see **percentage-point** vs **relative** rules in system Section 2. **Rolling average:** value is the **rolling baseline**; contrast with **current_kpi_value** as **current − feature_value**, not vs prev_kpi_value.
dax_kpi_query — DAX used for the KPI that triggered the signal.
dax_feature_query — DAX used for the feature/driver calculation.
current_kpi_value — KPI level in the **current** window (same as logical “current”; fractional rates may be 0–1; see level_now).
prev_kpi_value — KPI level in the **previous comparison window** (same as level_prior). **Monthly example:** current window 1–21 Mar 2026 → prev is **1–21 Feb 2026**. **Weekly example:** current 15–21 Mar 2026 → prev is **9–15 Mar 2026**. Align prose to ``period_window`` when present.
level_now / level_prior — Display-formatted current vs **prior-period** KPI levels; prior uses the same calendar rules as ``prev_kpi_value``.

─────────────────────────────────────────────────────── WHY ROWS Each row: why_id | signal_id | driver_metric | driver_slice_dimension | driver_slice_value | dep_kpi_name (null = inter-dimensional; non-null = upstream KPI) | current | prev | change_pct | rationale ─────────────────────────────────────────────────────── {{WHY_ROWS}}

─────────────────────────────────────────────────────── WHY / why_results COLUMN GLOSSARY (reference) ───────────────────────────────────────────────────────
why_id — Unique id for this explanation row.
signal_id — Parent signal this WHY row explains.
run_timestamp — When the WHY pipeline ran for this signal.
kpi_name — KPI the WHY explains (e.g. gross_close_rate).
dimension_name — Business dimension of the slice (e.g. Market, Channel).
dimension_value — Member affected (e.g. Richmond).
signal_name — Pattern that fired (e.g. strong_deceleration, weekly_degrowth).
dep_kpi_name — Driver KPI name when the row is KPI-chain dependency; null for inter-dimensional rows. Do not echo these as ``Route 1`` / ``Route 2`` in narrative output.
dep_kpi_label — Human-readable driver label; optional.
rationale — Short natural-language summary of the movement / observation.
current_value — KPI level in the **current** anomaly window (e.g. 0.4 → 40% when rate).
prev_value — **Prior comparison window** KPI level when the WHY is **growth / period-over-period** (current vs calendar prior). **Not** always the right baseline for rolling-average stories — those may compare current to the rolling baseline on the signal/feature side. **Monthly:** if current is 1–21 Mar 2026, prev window is **1–21 Feb 2026**. **Weekly:** if current is 15–21 Mar 2026, prev window is **9–15 Mar 2026**.
change_pct — **Not always** “current minus previous.” **Growth-style WHY:** typically derived from **current_value vs prev_value** (or % change from that pair; for some rate KPIs use **percentage points** — see system Section 2). **Rolling / vs-baseline WHY:** may reflect **current vs rolling baseline** (e.g. current minus feature baseline from the signal pipeline), not current − prev_value. Use ``signal_name``, ``feature_name``, and rationale to choose the correct interpretation.
period — Human-readable **current** evaluation window (e.g. 2026-03-15 to 2026-03-21).
period_start / period_end — **Current** window date bounds for this WHY row; the **previous** window is the same span shifted per monthly/weekly rules above.
created_at — When the row was persisted (audit / lineage).

─────────────────────────────────────────────────────── CORRELATION PRIORS (fixed prior elasticities; cite only when aligned with signal/WHY evidence) ─────────────────────────────────────────────────────── {{CORRELATION_PRIORS}}

───────────────────────────────────────────────────────
ANOMALY TIMELINE CONTEXT
(none if unavailable)
───────────────────────────────────────────────────────
{{ANOMALY_TIMELINE}}

─────────────────────────────────────────────────────── OUTPUT CHECKLIST (verify before returning JSON) ───────────────────────────────────────────────────────
[ ] Title includes dimension value or name + at least one number + trade-off framing
[ ] Title and insight do not overlap — title is headline only; insight adds mechanism and journey
[ ] Insight sourced from signals only — no WHY reasoning mixed in
[ ] insight_summary is 3-4 plain-language sentences with prefixes (KPI/metric/business unit), no causal language
[ ] Why opens with a causal bridge, covers KPI-dependency drivers before dimensional breakdown — no ``Route 1`` / ``Route 2`` labels
[ ] Why includes the required cross-dimension breakdown for this primary dimension
[ ] why_insight_summary bullets explain causes — not a repeat of insight_summary
[ ] why_insight_summary has upstream-KPI and dimensional bullets when both types exist — no ``(Route 1)`` / ``(Route 2)`` suffixes
[ ] impact_insight uses conviction language — no hedging words
[ ] All rate metrics displayed as percentages (never as decimals)
[ ] ``title`` has no asterisks at all; body fields use well-formed ``**…**`` pairs, ``•`` bullets only, no line-start ``*``
[ ] No pipeline jargon (``WHY row``, ``signal row``, etc.) — executive wording only (Section 2)
[ ] At least one impact_insight bullet cites a regressor → target from CORRELATION PRIORS when applicable
[ ] Zero underscored tokens anywhere in any field
[ ] Story reflects the actual primary signal — not defaulted to cost → CPL → close rate
[ ] When several KPIs appear in SIGNAL ROWS: title and insight opening do not ignore set rate, demo rate, or average ticket size if those rows are present with meaningful movement — avoid leading only with cost per demo / close rates out of habit
[ ] Every WHY row is represented in the why narrative and why_insight_summary
[ ] All bullets are 10–12 words maximum
[ ] All prose sentences are 25 words or fewer
[ ] JSON is valid with no unescaped double-quotes inside string values
"""

_ONE_SHOT_DIMENSIONAL_MAIN_SYSTEM = (
    "You are writing a dimensional insight for one slice (dimension name — one dimension value), "
    "one WHY period bucket (weekly vs monthly window from ``why_results``, e.g. 15–21 Mar vs 1–21 Mar), "
    "where multiple distinct measures appear in SIGNAL ROWS. Integrate across all those measures and "
    "all WHY drivers for that period bucket only; this is not a single-metric alert story.\n\n"
    "━━━━━━━━━━━━━━━━ DIMENSIONAL — HEADLINE KPI BALANCE (MANDATORY) ━━━━━━━━━━━━━━━\n"
    "SIGNAL ROWS often combine cost per demo, cost per lead, net close rate, gross close rate, "
    "issue rate, set rate, demo rate, average ticket size, and similar measures. Stakeholder "
    "feedback: titles and openings over-index on cost per demo and close rates while set rate, "
    "demo rate, and average ticket size too rarely headline even when they have real signals.\n"
    "- Title: Pick the lead measure in the title by strategic distinctiveness for this slice, "
    "not by habitually naming cost per demo or a close rate first. When set rate, demo rate, or "
    "average ticket size appears in SIGNAL ROWS with a material movement (same order of importance as "
    "other alerts in the cluster), put that measure first in the title (or lead with an explicit "
    "pairing such as ticket-size tension with a close-rate move). Reserve cost-per-demo / net or gross "
    "close rate for the title lead only when they are clearly the dominant breach or the only severe story.\n"
    "- Insight first paragraph: Match the title’s emphasis — do not bury set rate, demo rate, or "
    "average ticket size after several sentences on cost or close rates when those metrics also fired.\n"
    "- Still mandatory: Every claim traces to SIGNAL / WHY data; never invent a KPI movement. "
    "Title: plain text, no asterisks. Other JSON narrative fields: **double-asterisk** pairs and • bullets per system Section 2.\n\n"
) + _NARRATIVE_SYSTEM_PROMPT

_Portal_PIPELINE_BUSINESS_CONTEXT = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION 1 — BUSINESS MODEL: Portal B2B SALES PIPELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A B2B opportunity pipeline from first lead through stage progression, commit
discipline, and closed revenue. Movement is tracked weekly on ``DimCalendar``
(week-start dates). Insights should speak to pipeline health, execution velocity,
forecast reliability, and deal economics — not retail home-improvement funnel jargon
unless those metrics literally appear in SIGNAL ROWS.

CORE OBJECTS
- Opportunity / Potential — active deal in the pipeline with stage, owner, type, and value.
- Lead — inbound or sourced demand (OEM, Own, Management, and related sources).
- Commit — forecasted close timing; slips and month-bound commit counts matter for reliability.

PRIMARY KPI FAMILIES (use plain English labels in narrative)
- Lead volume — OEM Leads, Own Leads, Management Leads.
- Pipeline scale — Open Opportunities, Pipeline Value, Weighted Pipeline Value.
- Deal economics — Avg Deal Size, Approved Funding Amount.
- Stage execution — Stage Moved, Stage Current Week, Stage Last Week; stalled deals when
  movement is absent.
- Forecast / commit — Current Month Commit Count, Next Month Commit Count, Commit Slipped Count.
- Capacity — Sales Rep Count (coverage vs load).

ANALYTICAL DIMENSIONS (slice breakdowns in WHY rows)
- Potential Type, Opportunity Type, Industry Head, Stage, Sales Rep — and other configured
  Portal dimensions. When multiple slices fire on the same KPI, explain how they combine or
  offset; do not narrate only the loudest slice.

INTERPRETIVE LENSES (only when evidence supports)
- Volume vs quality — more leads or opportunities without value follow-through.
- Velocity — stage movement week-over-week; bottlenecks at specific stages or rep slices.
- Forecast risk — commit slips, weak next-month commit cover, slipped counts rising.
- Economics — pipeline value vs weighted value (probability-adjusted), ticket / deal size shifts.
- Coverage — rep count or concentration risk when Sales Rep slices dominate signals.

SIGNAL TYPES IN THIS Portal FORK
- Week-over-week (WoW) growth / degrowth — compare current week to the immediately prior week
  of equal length.
- Portfolio raw KPI (SELF) — absolute level vs a configured threshold.
- Rolling / baseline features — describe as rolling or baseline comparison when feature_label implies it.
"""

_NARRATIVE_SYSTEM_PROMPT_FROM_SECTION_2 = (
    "SECTION 2 — SIGNAL INTERPRETATION RULES"
    + _NARRATIVE_SYSTEM_PROMPT.split("SECTION 2 — SIGNAL INTERPRETATION RULES", 1)[1]
)

_Portal_NARRATIVE_SYSTEM_PROMPT = (
    "You are an insight writer for a Portal / B2B sales pipeline intelligence portal. "
    "You turn signal rows and WHY drivers into a clear, reasoned story for sales leaders, "
    "revops, and executives. Plain English first, numbers second, one idea at a time.\n\n"
    + _Portal_PIPELINE_BUSINESS_CONTEXT
    + "\n\n"
    + _NARRATIVE_SYSTEM_PROMPT_FROM_SECTION_2
)

_Portal_NARRATIVE_CORRELATION_PRIORS_JSON = "[]"

_ONE_SHOT_KPI_ROLLUP_MAIN_SYSTEM = (
    "You are writing a **KPI rollup** insight for **one measure** (one ``kpi_name``) within "
    "one WHY period bucket (weekly vs monthly window from ``why_results``), where SIGNAL ROWS "
    "span **multiple Portal analytical dimensions** "
    "(e.g. Potential Type, Opportunity Type, Industry Head, Stage, Sales Rep). Integrate "
    "across every dimension slice and every WHY driver for that KPI — this is not a single-slice alert.\n\n"
    "━━━━━━━━━━━━━━━━ KPI ROLLUP — NARRATIVE RULES (MANDATORY) ━━━━━━━━━━━━━━━━\n"
    "- Title: Plain text only (no asterisks). Lead with the KPI plain-English name and the "
    "headline movement for the period.\n"
    "- insight / problem_statement: 2-4 paragraphs; **bold** every metric, slice, and date; "
    "close with the period window (e.g. Period window: **2026-06-22** to **2026-06-28**).\n"
    "- Do **not** use legacy Client home-services funnel language unless those exact metrics "
    "appear in SIGNAL / WHY data.\n\n"
) + _Portal_NARRATIVE_SYSTEM_PROMPT + (
    "\n\n━━━━━━━━━━━━━━━━ KPI ROLLUP — PORTAL FORMAT (OVERRIDES SECTION 13) ━━━━━━━━━━━━━━━━\n"
    "Persisted ``main_insights`` columns must match generate-dimensional rows exactly.\n\n"
    "**why** (string):\n"
    "1) Opening paragraph — one causal bridge (no subsection header).\n"
    "2) Then 2-3 themed subsections. Each subsection:\n"
    "   <sub>**Theme — Observation phrase**</sub>   (em dash between theme and observation)\n"
    "   (blank line)\n"
    "   • Bullet with **bold** metrics and driver attribution\n"
    "   • Another bullet …\n"
    "   (blank line before next <sub> block)\n\n"
    "**insight_summary** — OVERRIDE Section 13: use • bullet lines separated by \\n, NOT prose "
    "sentences. Example:\n"
    "\"• Set rate rose **16 pp**, from **12%** to **28%**, in **A.East Florida**\\n"
    "• Issue rate fell **16 pp**, from **51%** to **35%**, same period\"\n\n"
    "**why_insight_summary** — • bullet lines separated by \\n (root causes only). Example:\n"
    "\"• Set count up **157%**; revshare spend doubled, lifting set rate\\n"
    "• Sales rep count down **10%**; fewer reps constrained issuance capacity\"\n\n"
    "**impact_summary** — • bullets, 3-5 lines, consequences and risks.\n"
)

_NARRATIVE_WHY_SHARED_RAW_MAX_CHARS = 200_000
# Dimensional clusters pack many WHY rows into JSON; the one-shot prompt uses JSON + preamble,
# so it needs a higher ceiling than the line-oriented standard path (override via env).
_NARRATIVE_WHY_DIMENSIONAL_DEFAULT_MAX_CHARS = 200_000


def _narrative_why_raw_max_chars(*, dimensional: bool = False) -> int:
    """Max raw WHY payload chars (lines or JSON) for clustering gate and one-shot narrative."""
    raw = (os.getenv("INSIGHTS_NARRATIVE_WHY_MAX_CHARS") or "").strip()
    if raw:
        try:
            return max(1024, int(raw))
        except ValueError:
            pass
    if dimensional:
        raw_d = (os.getenv("INSIGHTS_NARRATIVE_WHY_DIMENSIONAL_MAX_CHARS") or "").strip()
        if raw_d:
            try:
                return max(1024, int(raw_d))
            except ValueError:
                pass
        return _NARRATIVE_WHY_DIMENSIONAL_DEFAULT_MAX_CHARS
    return _NARRATIVE_WHY_SHARED_RAW_MAX_CHARS


def _is_placeholder_dimension_value(dv: str) -> bool:
    t = (dv or "").strip().upper()
    return t in ("", "NA", "N/A", "NULL")


def _canonical_slice_dimension_value(bucket_dv: str, signals: list[Signal]) -> str:
    """Prefer real ``signal_log.dimension_value`` from loaded signals over SQL bucket when bucket is NA/empty."""
    vals = {
        (s.dimension_value or "").strip()
        for s in signals
        if (s.dimension_value or "").strip() and not _is_placeholder_dimension_value(s.dimension_value)
    }
    if len(vals) == 1:
        return next(iter(vals))
    b = (bucket_dv or "").strip()
    if not _is_placeholder_dimension_value(b):
        return b
    if len(vals) > 1:
        return ", ".join(sorted(vals))[:400]
    return b


def _narrative_max_output_tokens(
    provider: Literal["openai", "cohere_azure"] | None = None,
) -> int:
    """Completion token budget; Cohere Command A+ supports up to 64K output tokens."""
    # Cohere Command A+ (05-2026) max generation per model card.
    _COHERE_COMMAND_A_PLUS_MAX_OUTPUT = 65536

    raw = (os.getenv("INSIGHTS_NARRATIVE_MAX_OUTPUT_TOKENS") or "").strip()
    if raw:
        try:
            return max(256, int(raw))
        except ValueError:
            pass
    if provider == "cohere_azure":
        cohere_raw = (os.getenv("INSIGHTS_COHERE_MAX_OUTPUT_TOKENS") or "").strip()
        if cohere_raw:
            try:
                return max(256, min(_COHERE_COMMAND_A_PLUS_MAX_OUTPUT, int(cohere_raw)))
            except ValueError:
                pass
        return _COHERE_COMMAND_A_PLUS_MAX_OUTPUT
    return 4096


def _cohere_thinking_payload() -> dict[str, Any] | None:
    """Command A+ spends the full output budget on thinking unless disabled."""
    mode = (os.getenv("INSIGHTS_COHERE_THINKING") or "disabled").strip().lower()
    if mode in ("off", "false", "0", "disabled", "disable", "none"):
        return {"type": "disabled"}
    if mode in ("on", "true", "1", "enabled", "enable"):
        return {"type": "enabled"}
    return {"type": "disabled"}


def _extract_cohere_assistant_text(message: dict[str, Any], choice: dict[str, Any]) -> str:
    """Azure Cohere chat completions — content may be empty when reasoning fills the budget."""
    raw_content = message.get("content")
    if isinstance(raw_content, list):
        text_parts: list[str] = []
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                txt = block.get("text") or block.get("content")
                if isinstance(txt, str) and txt.strip():
                    text_parts.append(txt.strip())
        if text_parts:
            return "\n".join(text_parts)
    if isinstance(raw_content, str) and raw_content.strip():
        return raw_content.strip()
    for key in ("content", "reasoning_content", "reasoning", "text"):
        val = message.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("content", "text"):
        val = choice.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _split_list_into_k_chunks(items: list, k: int) -> list[list]:
    """Split items into k non-empty slices (sizes differ by at most 1)."""
    n = len(items)
    if n == 0:
        return []
    if k <= 1:
        return [items]
    k = min(k, n)
    base, rem = divmod(n, k)
    out: list[list] = []
    idx = 0
    for i in range(k):
        sz = base + (1 if i < rem else 0)
        out.append(items[idx : idx + sz])
        idx += sz
    return [c for c in out if c]


def _signal_lines_for_narrative(
    signals: list[Signal],
    why_map: Optional[dict[str, list[WhyRow]]] = None,
) -> list[str]:
    lines = []
    for s in signals:
        fl = _feature_label_for_prompt(s)
        sig_nm = (s.signal_name or "").strip() or "—"
        lines.append(
            f"metric {s.kpi_name} | feature_label {fl} | signal_name {sig_nm} | signal_id {s.signal_id} "
            f"| dim {s.dimension} — {s.dimension_value} | feature_value {_fmt_feature_or_observed_for_prompt(s.feature_value)} "
            f"| observed {_fmt_feature_or_observed_for_prompt(s.observed_value)} | level_now {_fmt_kpi_level_for_prompt(s.kpi_name, s.current_kpi_value)} "
            f"| level_prior {_fmt_kpi_level_for_prompt(s.kpi_name, s.prev_kpi_value)}"
            f"{_signal_window_suffix(s.signal_id, why_map)}"
        )
    return lines


def _sort_why_rows_for_narrative_driver_first(rows: list[WhyRow]) -> list[WhyRow]:
    """Group WHY rows by dependency driver first so **driver_metric** / dep KPI names are not
    buried under long runs of the same slice pattern (e.g. many lines sharing one %).
    """
    if not rows:
        return rows

    def _key(w: WhyRow) -> tuple[str, str, str, str, str]:
        dn = (w.dep_kpi_name or "").strip().lower()
        dl = (w.dep_kpi_label or "").strip().lower()
        dname = (w.dimension_name or "").strip().lower()
        dval = (w.dimension_value or "").strip().lower()
        sid = str(w.signal_id)
        return (dn, dl, dname, dval, sid)

    return sorted(rows, key=_key)


def _distinct_dep_driver_preamble(
    rows: list[WhyRow], *, max_list_chars: int = 2000, max_list_items: int = 120
) -> str:
    """One-line reminder of unique dependency drivers for LLM coverage (not a data row)."""
    if not rows:
        return ""
    seen: set[tuple[str, str]] = set()
    labels: list[str] = []
    for w in rows:
        dn = (w.dep_kpi_name or "").strip()
        dl = (w.dep_kpi_label or "").strip()
        if not dn and not dl:
            continue
        t = (dn, dl)
        if t in seen:
            continue
        seen.add(t)
        if dn and dl and dn != dl:
            labels.append(f"{dl} (id={dn})")
        else:
            labels.append(dl or dn)
        if len(labels) >= max_list_items:
            break
    if not labels:
        return ""
    joined = ", ".join(labels)
    if len(joined) > max_list_chars:
        joined = joined[: max_list_chars - 1] + "…"
    return (
        "COVERAGE: Name each distinct driver_metric below in the merged text (not only geographic or channel slices). "
        f"Distinct driver_metric / dependent_driver_id pairs in this inventory ({len(seen)}): {joined}"
    )


def _why_lines_for_narrative(
    sids: list[str],
    why_map: dict[str, list[WhyRow]],
    *,
    rationale_max: Optional[int] = None,
    max_per_signal: Optional[int] = None,
    max_total_lines: Optional[int] = None,
) -> list[str]:
    coll: list[WhyRow] = []
    for sid in sids:
        rows = why_map.get(sid) or []
        if max_per_signal is not None and max_per_signal > 0:
            rows = rows[:max_per_signal]
        coll.extend(rows)
    coll = _sort_why_rows_for_narrative_driver_first(coll)
    lines: list[str] = []
    for w in coll:
        if max_total_lines is not None and len(lines) >= max_total_lines:
            break
        full_rat = w.rationale or ""
        rat = full_rat if rationale_max is None else full_rat[:rationale_max]
        dl = (w.dep_kpi_label or "").strip() or "—"
        dn = (w.dep_kpi_name or "").strip() or "—"
        dslice = f"{(w.dimension_name or '').strip()} — {(w.dimension_value or '').strip()}"
        lines.append(
            f"WHY | main_metric={w.kpi_name} | signal_id={w.signal_id} | "
            f"driver_metric {dl} (dependent_driver_id={dn}) | "
            f"driver_slice {dslice} | "
            f"change_pct={w.change_pct}% | "
            f"current={format_metric_value_for_display(_why_driver_metric_label(w), w.current_value)} | "
            f"prev={format_metric_value_for_display(_why_driver_metric_label(w), w.prev_value)} | "
            f"Rationale: {rat}"
        )
    return lines


def _signal_period_window_token(signal_id: str, why_map: dict[str, list[WhyRow]]) -> str:
    """Compact period window string for JSON (matches PERIOD WINDOWS BY SIGNAL_ID)."""
    suf = _signal_window_suffix(signal_id, why_map)
    if "period_window n/a" in suf:
        return "n/a"
    return suf.split("period_window", 1)[-1].strip()


def _signals_json_for_prompt(
    signals: list[Signal],
    why_map: dict[str, list[WhyRow]],
) -> str:
    """JSON array mirroring ``json_agg`` over ``signal_log`` columns plus display helpers."""
    rows: list[dict] = []
    for s in signals:
        fl = _feature_label_for_prompt(s)
        rows.append(
            {
                "signal_id": s.signal_id,
                "kpi_name": s.kpi_name,
                "dimension": s.dimension,
                "dimension_value": s.dimension_value,
                "observed_value": s.observed_value,
                "threshold_value": s.threshold_value,
                "breach_delta": s.breach_delta,
                "signal_name": s.signal_name,
                "feature_name": s.feature_name,
                "feature_label": fl,
                "feature_value": s.feature_value,
                "current_kpi_value": s.current_kpi_value,
                "prev_kpi_value": s.prev_kpi_value,
                "level_now": _fmt_kpi_level_for_prompt(s.kpi_name, s.current_kpi_value),
                "level_prior": _fmt_kpi_level_for_prompt(s.kpi_name, s.prev_kpi_value),
                "period_window": _signal_period_window_token(s.signal_id, why_map),
            }
        )
    return json.dumps(rows, ensure_ascii=False)


def _why_rows_json_for_prompt(rows: list[WhyRow]) -> str:
    """JSON array mirroring ``json_agg`` over ``why_results`` (plus ``driver_metric`` + ``rationale``)."""
    out: list[dict] = []
    for w in rows:
        dm = (w.dep_kpi_label or "").strip() or (w.dep_kpi_name or "").strip() or None
        rat = (w.rationale or "").strip()
        period = (w.period or "").strip()
        pstart = w.period_start.isoformat() if w.period_start is not None else None
        pend = w.period_end.isoformat() if w.period_end is not None else None
        out.append(
            {
                "signal_id": w.signal_id,
                "kpi_name": w.kpi_name,
                "dimension_name": w.dimension_name,
                "dimension_value": w.dimension_value,
                "signal_name": w.signal_name,
                "dep_kpi_name": w.dep_kpi_name,
                "driver_metric": dm,
                "current_value": w.current_value,
                "prev_value": w.prev_value,
                "change_pct": w.change_pct,
                "period": period or None,
                "period_start": pstart,
                "period_end": pend,
                "rationale": rat or None,
            }
        )
    return json.dumps(out, ensure_ascii=False)


def _narrative_pipe_safe_text(s: str) -> str:
    """Single-line, pipe-safe snippet for delimiter-separated prompt rows."""
    return (s or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _signal_observed_direction(s: Signal) -> str:
    """Heuristic direction from current vs prior KPI level (not the alert observed_value)."""
    c, p = s.current_kpi_value, s.prev_kpi_value
    if c is None or p is None:
        return "n/a"
    try:
        fc, fp = float(c), float(p)
    except (TypeError, ValueError):
        return "n/a"
    if fc > fp:
        return "up"
    if fc < fp:
        return "down"
    return "flat"


def _signal_rows_for_narrative_user_prompt(
    signals: list[Signal],
    why_map: dict[str, list[WhyRow]],
) -> str:
    """Pipe-separated rows aligned with the one-shot user prompt SIGNAL ROWS spec."""
    if not signals:
        return "(no signals)"
    lines: list[str] = []
    for s in signals:
        pw = _signal_period_window_token(s.signal_id, why_map)
        lines.append(
            f"{s.signal_id} | {s.kpi_name or 'n/a'} | {_feature_label_for_prompt(s)} | "
            f"{_fmt_kpi_level_for_prompt(s.kpi_name, s.current_kpi_value)} | "
            f"{_fmt_kpi_level_for_prompt(s.kpi_name, s.prev_kpi_value)} | "
            f"{_fmt_feature_or_observed_for_prompt(s.feature_value)} | {pw} | {_signal_observed_direction(s)}"
        )
    return "\n".join(lines)


def _why_rows_for_narrative_user_prompt(rows: list[WhyRow]) -> str:
    """Pipe-separated rows aligned with the one-shot user prompt WHY ROWS spec."""
    if not rows:
        return "(no WHY rows)"
    lines: list[str] = []
    for w in rows:
        wid = ((w.why_id or "").strip() or "n/a")
        dm = (w.dep_kpi_label or "").strip() or (w.dep_kpi_name or "").strip() or "n/a"
        dep = w.dep_kpi_name
        dep_disp = "null" if dep is None or str(dep).strip() == "" else str(dep).strip()
        cv = format_metric_value_for_display(_why_driver_metric_label(w), w.current_value)
        pv = format_metric_value_for_display(_why_driver_metric_label(w), w.prev_value)
        cp = "n/a" if w.change_pct is None else str(w.change_pct)
        rat = _narrative_pipe_safe_text(w.rationale or "")
        lines.append(
            f"{wid} | {w.signal_id} | {dm} | {(w.dimension_name or 'n/a')} | {(w.dimension_value or 'n/a')} | "
            f"{dep_disp} | {cv} | {pv} | {cp} | {rat}"
        )
    return "\n".join(lines)


def _narrative_period_template_fields(
    cluster: SignalCluster,
    why_map: dict[str, list[WhyRow]],
) -> tuple[str, str, str]:
    """period_type and one window (or spanning bounds) for the user prompt PERIOD CONTEXT block."""
    period_type = (cluster.period or "").strip() or "n/a"
    if cluster.period_start is not None and cluster.period_end is not None:
        return (
            period_type,
            cluster.period_start.isoformat(),
            cluster.period_end.isoformat(),
        )
    wins = _period_windows_for_cluster(cluster, why_map)
    if len(wins) == 1:
        s, e = wins[0]
        return period_type, s.isoformat(), e.isoformat()
    ps, pe = _period_bounds_for_cluster(cluster, why_map)
    if ps is not None and pe is not None:
        return period_type, ps.isoformat(), pe.isoformat()
    return period_type, "n/a", "n/a"


def _distinct_kpi_names_from_signals(signals: list[Signal]) -> list[str]:
    """Sorted unique ``kpi_name`` values for cross-KPI headline reminders."""
    return sorted(
        {(s.kpi_name or "").strip() for s in signals if (s.kpi_name or "").strip()},
        key=str.lower,
    )


def _cross_kpi_headline_balance_block(signals: list[Signal]) -> str:
    """Extra user-message block when a cluster has multiple KPIs (dimensional insights)."""
    kpis = _distinct_kpi_names_from_signals(signals)
    if len(kpis) < 2:
        return ""
    listed = ", ".join(kpis)
    return (
        "\n\n──────────────────────── CROSS-KPI HEADLINE BALANCE ────────────────────────\n"
        f"Distinct KPIs with signals in this cluster: {listed}.\n"
        "When choosing the title and the first paragraph of insight, do not automatically "
        "lead with cost per demo, net close rate, or gross close rate. If set rate, "
        "demo rate, or average ticket size is in the list above and has a substantive signal "
        "row, give that measure headline or co-equal billing unless cost/close is clearly the only "
        "severe story. This corrects portfolio-wide underrepresentation of funnel-early conversion "
        "and ticket-quality narratives. Wrap numbers and labels in **…** in JSON per system Section 2.\n"
    )


def _fill_narrative_user_prompt_one_shot(
    *,
    cluster: SignalCluster,
    cluster_sigs: list[Signal],
    why_rows: list[WhyRow],
    why_eff: dict[str, list[WhyRow]],
) -> str:
    """Materialize ``_NARRATIVE_USER_PROMPT_TEMPLATE`` with cluster / signal / WHY data."""
    period_type, p_start, p_end = _narrative_period_template_fields(cluster, why_eff)
    signal_block = _signal_rows_for_narrative_user_prompt(cluster_sigs, why_eff)
    why_block = _why_rows_for_narrative_user_prompt(why_rows)
    t = _NARRATIVE_USER_PROMPT_TEMPLATE
    cluster_theme = (
        f"{cluster.kpi_name} | {cluster.dimension_name} — {cluster.dimension_value}"
        if (cluster.kpi_name or cluster.dimension_name or cluster.dimension_value)
        else "n/a"
    )
    replacements: dict[str, str] = {
        "{{CLUSTER_THEME}}": cluster_theme,
        "{{DIMENSION_NAME}}": (cluster.dimension_name or "n/a"),
        "{{DIMENSION_VALUE}}": (cluster.dimension_value or "n/a"),
        "{{PERIOD_TYPE}}": period_type,
        "{{PERIOD_START}}": p_start,
        "{{PERIOD_END}}": p_end,
        "{{SIGNAL_ROWS}}": signal_block,
        "{{WHY_ROWS}}": why_block,
        "{{CORRELATION_PRIORS}}": (
            _Portal_NARRATIVE_CORRELATION_PRIORS_JSON
            if (cluster.cluster_type or "").lower() == "kpi_rollup"
            else _NARRATIVE_CORRELATION_PRIORS_JSON
        ),
        "{{ANOMALY_TIMELINE}}": "none",
    }
    for key, val in replacements.items():
        t = t.replace(key, val)
    extra = _cross_kpi_headline_balance_block(cluster_sigs)
    if (cluster.cluster_type or "").lower() == "kpi_rollup":
        extra += _kpi_rollup_cross_dimension_block(cluster_sigs)
        extra += _kpi_rollup_portal_format_user_block()
    return t + extra


def _parse_narrative_json(raw: str) -> dict:
    """Parse narrative LLM output; fall back to ``json_repair`` when strict JSON fails."""
    s = raw.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3]
    s = s.strip()
    try:
        parsed: object = json.loads(s)
    except json.JSONDecodeError as e:
        try:
            parsed = json_repair_loads(s)
        except Exception as e2:
            head = s[:800].replace("\n", " ")
            logger.warning(
                "Narrative JSON strict parse failed (%s); json_repair failed (%s). Head: %s",
                e,
                e2,
                head,
            )
            raise e2 from e
        logger.info(
            "Narrative JSON recovered via json_repair (was: %s)",
            str(e).split("\n", 1)[0][:200],
        )
    if not isinstance(parsed, dict):
        raise ValueError("Narrative JSON root must be a JSON object")
    return parsed


_MAIN_INSIGHT_MAX_TAGS = 3
_MAIN_INSIGHT_TITLE_HARD_MAX_WORDS = 18
_BULLET_FIELD_MAX_ITEMS = 30

# When the model returns fewer than three actions, pad remaining slots (only after at least one real action).
_RECOMMENDED_ACTION_PAD: tuple[str, ...] = (
    "Align the accountable owners on the metric and slice above, with a dated decision on the next corrective step.",
    "Pressure-test the two largest downside drivers with finance and operations before the next leadership readout.",
    "Track a small set of weekly leading indicators until the pattern stabilizes or a mitigation plan is in market.",
)


def _normalize_tags_for_main_insight(tags: object) -> str:
    """Cap at ``_MAIN_INSIGHT_MAX_TAGS`` tags; ``MainInsight.tags`` is one comma-separated string."""
    items: list[str] = []
    if isinstance(tags, list):
        items = [str(x).strip() for x in tags if x is not None and str(x).strip()]
    elif isinstance(tags, str):
        items = [p.strip() for p in tags.replace(";", ",").split(",") if p.strip()]
    elif tags is not None:
        s = str(tags).strip()
        if s:
            items = [s]
    return ", ".join(items[:_MAIN_INSIGHT_MAX_TAGS])


# Raw ``signal_log.dimension`` / config names often look like ``Branch_Name``; strip underscores for readers.
_SNAKE_LABEL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b")


def _humanize_snake_case_labels(text: Optional[str]) -> Optional[str]:
    """Replace ``Word_Word`` tokens with spaced words (``Branch Name``) for UI copy."""

    def _repl(m: re.Match[str]) -> str:
        raw = m.group(1)
        parts = [p for p in raw.split("_") if p]
        if not parts:
            return raw
        return " ".join(p.capitalize() for p in parts)

    if text is None:
        return None
    s = str(text)
    if not s.strip():
        return text
    return _SNAKE_LABEL_RE.sub(_repl, s)


def _humanize_metric_label(ml: str) -> str:
    """Comma-separated metric names (cluster ``kpi`` field) → humanized segments for post-processing."""
    ml = (ml or "").strip()
    if not ml:
        return ""
    chunks: list[str] = []
    for part in ml.split(","):
        p = part.strip()
        if not p:
            continue
        h = _humanize_snake_case_labels(p)
        chunks.append(h if h is not None else p)
    return ", ".join(chunks)


def _replace_vague_kpi_phrasing(text: str, metric_label: Optional[str]) -> str:
    """Remove generic ``KPI`` wording when we can substitute the real metric name(s)."""
    if not text or not str(text).strip():
        return text
    ml_raw = (metric_label or "").strip()
    ml_display = _humanize_metric_label(ml_raw) if ml_raw else ""
    single = bool(ml_raw) and ("," not in ml_raw) and len(ml_raw) <= 240 and bool(ml_display)

    s = text
    if single:
        s = re.sub(r"\bcurrent KPIs?\b", f"current {ml_display}", s, flags=re.I)
        s = re.sub(r"\bprior KPIs?\b", f"prior {ml_display}", s, flags=re.I)
        s = re.sub(r"\bmain KPIs?\b", ml_display, s, flags=re.I)
        s = re.sub(r"\bthe KPIs?\b", ml_display, s, flags=re.I)
    else:
        s = re.sub(r"\bcurrent KPIs?\b", "current reading", s, flags=re.I)
        s = re.sub(r"\bprior KPIs?\b", "prior reading", s, flags=re.I)
        first_named = ""
        if ml_display:
            first_named = ml_display.split(",")[0].strip()
        s = re.sub(r"\bmain KPIs?\b", first_named or "the metrics", s, flags=re.I)
        s = re.sub(r"\bthe KPIs?\b", first_named or "the metrics", s, flags=re.I)

    s = re.sub(r"\bKPIs\b", "metrics", s, flags=re.I)
    if single:
        s = re.sub(r"\bKPI\b", ml_display, s, flags=re.I)
    else:
        s = re.sub(r"\bKPI\b", "metric", s, flags=re.I)
    return s


def _polish_prose(text: Optional[str], *, metric_label: Optional[str] = None) -> str:
    """Humanize schema tokens; scrub vague ``KPI`` copy (``metric_label`` = cluster / row metric name)."""
    if text is None:
        return ""
    s = str(text)
    h = _humanize_snake_case_labels(s)
    s = h if h is not None else s
    return _replace_vague_kpi_phrasing(s, metric_label)


# Em dash (U+2014) or ASCII hyphen-minus after "Route N"
_RD = r"[—\-]"

_ROUTE_LINE_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^\s*Route\s*2\s*{_RD}\s*KPI\s*dependency:\s*", re.I),
    re.compile(rf"^\s*Route\s*2\s*{_RD}\s*metric\s*dependency:\s*", re.I),
    re.compile(rf"^\s*Route\s*1\s*{_RD}\s*inter-dimensional:\s*", re.I),
    re.compile(rf"^\s*Route\s*2\s*{_RD}\s*", re.I),
    re.compile(rf"^\s*Route\s*1\s*{_RD}\s*", re.I),
)

# Same labels may appear mid-paragraph (not only line-start).
_ROUTE_INLINE_SCRUB: tuple[re.Pattern[str], ...] = (
    re.compile(rf"Route\s*2\s*{_RD}\s*KPI\s*dependency:\s*", re.I),
    re.compile(rf"Route\s*2\s*{_RD}\s*metric\s*dependency:\s*", re.I),
    re.compile(rf"Route\s*1\s*{_RD}\s*inter-dimensional:\s*", re.I),
)


def _scrub_pipeline_jargon_for_executives(text: str) -> str:
    """Replace analyst-only phrases (WHY row, signal row) with business-facing wording."""
    if not (text or "").strip():
        return text or ""
    s = str(text).replace("\r\n", "\n")
    replacements: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r",\s*but\s+one\s+WHY\s+rows?\s+shows\b", re.I),
            "; the rolling-baseline comparison shows",
        ),
        (
            re.compile(r"\bbut\s+one\s+WHY\s+rows?\s+shows\b", re.I),
            "but the rolling-baseline comparison shows",
        ),
        (re.compile(r"\bone\s+WHY\s+rows?\s+shows\b", re.I), "the analysis shows"),
        (re.compile(r"\bone\s+WHY\s+rows?\b", re.I), "one view"),
        (re.compile(r"\bone\s+why\s+rows?\b", re.I), "one view"),
        (re.compile(r"\bWHY\s+rows?\b", re.I), "drivers"),
        (re.compile(r"\bsignal\s+rows?\b", re.I), "alerts"),
        (re.compile(r"\bwhy_results\b", re.I), "the evidence"),
        (re.compile(r"\bsignal_log\b", re.I), "the readings"),
    )
    for rx, to in replacements:
        s = rx.sub(to, s)
    s = re.sub(r"  +", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_route_labels_for_main_insight(text: str) -> str:
    """Remove Route 1/2 scaffolding from narrative fields (model drift / legacy phrasing)."""
    if not (text or "").strip():
        return text or ""
    lines = str(text).replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        ln = line
        for rx in _ROUTE_LINE_PREFIX_PATTERNS:
            ln = rx.sub("", ln)
        ln = re.sub(r"\s*\(Route\s*[12]\)\s*", " ", ln, flags=re.I)
        ln = re.sub(r"  +", " ", ln).strip()
        out.append(ln)
    merged = "\n".join(out)
    for rx in _ROUTE_INLINE_SCRUB:
        merged = rx.sub("", merged)
    return merged.strip()


def _normalize_title_percent_not_percentage_points(title: str) -> str:
    """Product convention: titles use **%** only, not 'percentage points' or pp (UI / exec scan)."""
    if not (title or "").strip():
        return title or ""
    t = title
    t = re.sub(
        r"(\d+(?:\.\d+)?)\s+percentage\s+points?\b",
        r"\1%",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"(\d+(?:\.\d+)?)\s*pp\b", r"\1%", t, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", t).strip()


def _coerce_title_for_main_insight(
    title: str,
    *,
    metric_label: str = "",
    dimension_value: str = "",
) -> str:
    """Prefer concrete, non-vague titles; trim if the model returns an overly long headline."""
    t = (title or "").strip()
    if not t:
        return t

    t = _normalize_title_percent_not_percentage_points(t)

    # Remove vague phrasing that users cannot act on.
    t = re.sub(r"(?i)\bmixed\s+performance\b", "", t).strip(" -:,;")
    t = re.sub(r"\s{2,}", " ", t)

    vague_terms = ("mixed", "performance", "trend", "movement")
    t_words = [w.strip(".,:;!?").lower() for w in t.split()]
    only_vague = bool(t_words) and all(w in vague_terms for w in t_words)
    if not t or only_vague:
        ml = _humanize_metric_label(metric_label).split(",")[0].strip() if metric_label else "Business outcome"
        dv = (dimension_value or "").strip()
        if dv:
            t = f"{ml} shifts in {dv} require focused corrective actions"
        else:
            t = f"{ml} shifts require focused corrective actions"

    words = t.split()
    if len(words) <= _MAIN_INSIGHT_TITLE_HARD_MAX_WORDS:
        return t
    return " ".join(words[:_MAIN_INSIGHT_TITLE_HARD_MAX_WORDS])


def _bullet_text_to_items(s: str) -> list[str]:
    """Split free-form model text into plain bullet bodies (no leading •)."""
    if not (s or "").strip():
        return []
    t = (
        s.replace("\u00b6", "\n")
        .replace("¶", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    out: list[str] = []
    for blk in re.split(r"\n\s*\n+", t.strip()):
        for ln in blk.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            for part in re.split(r"\s*•\s*", ln):
                part = part.strip()
                if not part:
                    continue
                part = re.sub(r"^[-*–—]\s*", "", part).strip()
                if part:
                    out.append(part)
    return out


def _normalize_bullet_field(val: object, *, max_bullets: int = _BULLET_FIELD_MAX_ITEMS) -> Optional[str]:
    """One ``• `` bullet per line for ``insight_summary`` / ``why_insight_summary`` / ``impact_insight``."""
    if val is None:
        return None
    pieces: list[str] = []
    if isinstance(val, list):
        for x in val:
            if x is None:
                continue
            pieces.extend(_bullet_text_to_items(str(x)))
    else:
        pieces.extend(_bullet_text_to_items(str(val)))
    if not pieces:
        return None
    if max_bullets and len(pieces) > max_bullets:
        pieces = pieces[:max_bullets]
    return "\n".join(f"• {p}" for p in pieces)


def _narrative_text_or_bullets(
    val: object, *, max_bullets: int = _BULLET_FIELD_MAX_ITEMS
) -> Optional[str]:
    """Normalize list or string bullet fields to consistent ``• `` lines for the UI."""
    return _normalize_bullet_field(val, max_bullets=max_bullets)


def _narrative_text_field(val: object, fallback: str = "") -> str:
    """Coerce str | list to a single string (paragraphs joined) for long text fields."""
    if val is None:
        return fallback
    if isinstance(val, str):
        return val.strip() or fallback
    if isinstance(val, list):
        return "\n\n".join(str(x).strip() for x in val if str(x).strip()) or fallback
    return (str(val).strip() or fallback)


_MARKUP_FORMAT_SYSTEM_PROMPT = """You format stored analytics narratives for a BI portal. The UI only renders bold from paired double asterisks: **like this**.

You must preserve the source text exactly: same words, numbers, punctuation, line breaks, and bullet character •. Do not paraphrase, summarize, add, remove, or reorder anything.

Your only job is to insert or fix **…** spans so readers see emphasis on:
- Metrics: percentages, currency, counts, dates (including YYYY-MM-DD and ranges), percentage points (pp), signed changes (+15%, −20%).
- Dimensions and slice labels: use the provided dimension_name and dimension_value; also bold other clear business entities as they appear verbatim (markets, branches/divisions, lead sources, vendors, rep/team names, product lines) when they are proper names or coded labels in the text.

Rules:
- Return ONLY the formatted narrative. No markdown code fences, no preamble or commentary.
- Never use single-asterisk emphasis. Remove stray "* " immediately after a bullet • if present.
- Use ** only around spans that appear verbatim in the input (case and spelling must match the input for the wrapped substring).
- Do not put ** inside words; never wrap only the first letter (forbidden: ``**M**arket``). Wrap full words or phrases (e.g. ``**Market Type**``).
- Keep bullets as lines starting with • where they already do.
"""

# ─── REFINEMENT PROMPTS (Opus - post-generation polish) ──────────────────────────

_REFINE_WHAT_SYSTEM_PROMPT = """You are an insight writer. You have two tasks:

TASK 1: Write a plain-language executive summary paragraph (3-4 sentences) with **bold** formatting on key terms.
TASK 2: Reformat the existing insight_summary into concise, dot-separated bullet points (one fact per bullet).

Return valid JSON with exactly two fields:
{
  "executive_paragraph": "Your 3-4 sentence paragraph here with **bold** on key terms",
  "reformatted_bullets": "• First concise point\n• Second concise point\n• Third concise point"
}

No markdown fences. No commentary before or after. Just the JSON."""

_REFINE_WHAT_USER_PROMPT_TEMPLATE = """You have two tasks for this insight.

TASK 1 — EXECUTIVE PARAGRAPH:
Write a short 3-4 sentence executive paragraph summarizing the business context in plain language.
Rules:
- I don't want a bullet list of numbers. I want a summary of business context and the business output that our executive should be able to understand without using any jargons.
- Complete sentences, no more than three or four sentences, about the health of the business, starting from overall business growth. That means revenue, if available, any margin numbers, and then the key drivers for that business growth or business decline. This is followed by one or two statements about further information or details that might come out of it.
- The language used has to be something that a 21-year-old fresher can understand without using any jargon that is coming from the data model.
- In case we are using any KPI, please prefix it with the term KPI or metrics. If it is a business unit, please prefix it with the term business unit so that we can understand what the context is.
- Use **double-star bold** formatting on key metrics, numbers, KPI names, business units, and any important emphasis words throughout the paragraph.

TASK 2 — REFORMAT BULLETS:
Take the existing insight_summary below and split it into concise, separate bullet points.
Rules:
- Each logical fact/statement gets its own bullet starting with •
- Keep each bullet SHORT and concise — one metric movement or one key fact per bullet
- Preserve all numbers and bold formatting from the original
- Do NOT add new information — only reformat what already exists
- Typically 4-6 concise bullets
- Use \n between bullets in the JSON string

CURRENT INSIGHT DATA:
Title: {title}
KPI: {kpi}
Dimension: {dimension_name} — {dimension_value}
Severity: {severity}

Current problem_statement:
{insight}

Current insight_summary (REFORMAT THIS INTO CONCISE BULLETS):
{insight_summary}

Current why (for context only):
{why}

Current impact (for context only):
{impact_insight}

Return JSON with "executive_paragraph" and "reformatted_bullets".
"""

_REFINE_WHY_SYSTEM_PROMPT = """You are a senior business analyst refining the "why" causal narrative for executive consumption.

You will receive the full insight row. Your task: rewrite ONLY the why and why_insight_summary fields with perfect language quality and proper formatting.

Return valid JSON with exactly two fields:
{
  "why": "...",
  "why_insight_summary": "..."
}

No markdown fences. No commentary before or after."""

_REFINE_WHY_USER_PROMPT_TEMPLATE = """Refine the why section and why_insight_summary of this insight for executive reading quality.

RULES FOR why:
- 2-4 themed sections, each with: <sub>**theme header**</sub> on its own line, topic sentence, 1-3 branches (Confirmed or Partial only), inference sentence, route label in parentheses.
- Use \\n\\n between paragraphs within a theme.
- Bold all numbers, percentages, dimension values with **…**.
- Use <sub>**header text**</sub> format for theme headers.
- Strategic bold emphasis on key turning points.
- No jargon, no underscored tokens.
- Each theme opens with a plain-English sentence before numbers appear.

RULES FOR why_insight_summary:
- 2-4 bullets (lines starting with •), each naming a root cause and its funnel consequence.
- Must include at least 1 Route 2 bullet (upstream metric dependency) and at least 1 Route 1 bullet (channel/segment driver).
- Max 12 words per bullet.
- Format: "• Root cause → mechanism → funnel impact"
- Must NOT repeat what is in insight_summary.
- Bold key metrics and values.

CURRENT INSIGHT DATA:
Title: {title}
KPI: {kpi}
Dimension: {dimension_name} — {dimension_value}
Severity: {severity}

Current problem_statement (for context — do NOT rewrite):
{insight}

Current insight_summary (for context — do NOT rewrite):
{insight_summary}

Current why:
{why}

Current why_insight_summary:
{why_insight_summary}

Current impact (for context):
{impact_insight}

Return JSON with refined "why" and "why_insight_summary" only.
"""

_WHY_SUMMARY_SONNET_SYSTEM_PROMPT = """You are a senior business analyst summarizing sales funnel insights for a CFO audience.

Follow the user prompt exactly and return only the rewritten why text."""

_WHY_SUMMARY_SONNET_USER_PROMPT_TEMPLATE = """You are a senior business analyst summarizing sales funnel insights for a CFO audience. Your only task is to rewrite the "why" field of an insight into a tight, executive-grade causal explanation.

SALES FUNNEL CONTEXT
A linear conversion pipeline: Raw Leads → Set Appointment → Issued Lead → Demo → Sold.
Key metrics:
- Input: Raw lead volume, Set %
- Efficiency: Issue %, Demo %, GCP %
- Revenue quality: GSL $, Cancel %, BTD %
- Cost: Cost per Lead (CPL)
A spend cut reduces raw leads, cascading into fewer demos and closes. Revshare cuts disproportionately reduce costlier channels, shifting blended CPL.

WRITING RULES
1. Open with exactly one causal chain sentence: name the root cause and connect it directly to the primary KPI movement (e.g., "A revshare cut starved the top of funnel, mechanically lowering blended CPL across channels."). Do not restate the KPI change — it is already in the title.
2. Identify 2–3 root causes only. A cause is a decision, input change, or structural shift (e.g., spend cut, rep reduction, mix shift). A downstream metric moving as a result is NOT a cause — do not give it its own subsection.
3. Write 2–3 subsections, ordered by impact — most consequential cause first.
4. Each subsection header format: **Dimension — Observation** (e.g., **Spend — Revshare cut reduced blended CPL**, **Market — Sioux Falls and Fargo normalized from elevated base**)
5. Under each header: exactly 2–3 bullet lines using • only. Each line must answer WHY — the decision, input, or structural factor behind the change. Never restate what changed without naming the cause.
6. Limit to top 3 dimension values per subsection. Do not list every market, vendor, or subsource.
7. Every figure cited must support the single argument of that sentence. Do not mix figures from different arguments in the same sentence.
8. State implications directly — no analyst caveats, no hedging, no speculation beyond data-driven inference.

FORMATTING RULES (strictly enforced)
- Bold only: use **text** for subsection headers, KPI names, dimension names, and key metrics.
- Subsection header: one standalone line in the form **Dimension — Observation** (the system wraps it for the UI — do not output HTML or <sub> tags yourself).
- No markdown of any kind: no #, no -, no *, no single stars, no \\n escape sequences.
- Use real line breaks only.
- Bullets: use the • character only.
- Output only the rewritten why text — no JSON wrapper, no preamble, no extra commentary.

Rewrite the why field below into a concise CFO-facing explanation. Follow all system rules exactly.

Do NOT restate the KPI change — it is already captured in the insight title.
Do NOT create subsections for downstream effects already explained by a prior cause.
Every bullet must name a cause — not describe an outcome.

INSIGHT TITLE: {insight_title}

KPI: {kpi}

DIMENSION: {dimension_name} — {dimension_value}

WHY (raw):
{why}
"""


def _why_unescape_literal_newlines(text: str) -> str:
    """If the model emitted backslash-n per prompt spec, convert to real LF for validation/storage."""
    if not text:
        return text
    s = str(text)
    s = re.sub(r"\\r\\n", "\n", s)
    s = re.sub(r"\\n", "\n", s)
    return s


def _normalize_why_for_drift_check(text: str | None) -> str:
    """Normalize WHY text for structure-only drift checks.

    Keeps factual prose comparison while ignoring portal structure markers
    such as bullet prefixes and subsection wrappers.
    """
    if not text:
        return ""
    s = _why_unescape_literal_newlines(str(text)).replace("\r\n", "\n")
    # Unwrap subsection wrappers used by portal rendering.
    s = re.sub(r"(?is)<sub>\s*(.*?)\s*<sub>", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"(?is)<sub>\s*(.*?)\s*</sub>", r"\1", s, flags=re.DOTALL)
    # Treat list markers as formatting, not prose.
    s = re.sub(r"(?m)^\s*[\u2022\-\*]\s+", "", s)
    s = s.replace("\u2022", " ")
    # Normalize dash variants to reduce punctuation-only mismatches.
    s = s.replace("—", "-").replace("–", "-")
    return normalize_markup_plain_text(s)


_WHY_REFORMAT_STRUCTURE_SONNET_SYSTEM_PROMPT = """You reformat existing "why" narratives for a BI portal.

Return only the reformatted why text. No markdown code fences, no preamble.

Hard rules:
- Preserve every fact, number, currency amount, percentage, count, date, market name, vendor, and causal claim from the input. Do not add, drop, or reword substantive content. You may fix punctuation only when needed for readability.
- Follow the FORMATTING RULES in the user message exactly (including line breaks as specified).
- If you cannot preserve the factual content exactly, output the input unchanged while still applying the formatting rules where possible."""

_WHY_REFORMAT_STRUCTURE_SONNET_USER_PROMPT_TEMPLATE = """Reformat the WHY text below for the portal. Keep the same facts and causal story; reorganize into opening prose, <sub> subheaders, and • bullets as required.

STRUCTURE
- Opening: 1–2 short paragraphs of plain prose (no bullets), drawn only from the input.
- Subsections: each has one <sub> subheader line (see FORMATTING RULES) followed by • bullets (2–4 per subsection when the input supports it). Use an em dash (—) between the dimension/theme label and the observation inside the **…** text. Do not invent bullets or numbers.

FORMATTING RULES (strictly enforced)
- Bold only: use **text** for subheader content, KPI names, dimension names, and key metrics.
- No markdown of any kind: no #, no -, no *, no single stars.
- Bullets: use the • character only.
- Line breaks: use **real line breaks** (normal Enter/newline characters in the assistant output). When the API serializes the text to JSON, those appear as the escape sequence ``\\n`` — that is the same newline, not the two visible characters backslash + n and not ``/n``.
- Subheader format: wrap every subheader in <sub>**Header Text**<sub> exactly like this (same <sub> tag opens and closes).
- Spacing (blank line = hit Enter twice so there is an **empty line** between blocks):
  • After the opening prose paragraph(s): one blank line, then the first <sub> line.
  • After each <sub>**…**<sub> line: one blank line, then the • bullets for that subsection.
  • Between • bullet lines in the same subsection: single line break only (no blank line between bullets).
  • After the last • bullet of a subsection, before the next <sub> line: one blank line.
  • After the final • bullet of the entire response: end with a newline.
- Output only the rewritten why text — no JSON wrapper, no preamble, no extra commentary.

INSIGHT TITLE: {insight_title}

KPI: {kpi}

DIMENSION: {dimension_name} — {dimension_value}

WHY (reformat this):
{why}
"""

# ─── EXECUTIVE SUMMARY (portal — 5 pointers × monthly / weekly) ─────────────────

_EXECUTIVE_SUMMARY_SYSTEM_PROMPT = """You are a CFO-facing executive brief writer.

You receive main business insights for ONE reporting window (monthly OR weekly).
Synthesize them into ONE cross-cutting brief for that window only.

Return ONLY valid JSON with exactly this shape:
{"pointers": ["...", "...", "...", "...", "..."]}

Rules:
- Exactly 5 strings in the pointers array (no more, no fewer)
- Each pointer MUST be at most 10 words (hard limit — count words on spaces)
- Cover the most important themes across ALL insights in this window
- Plain business English; no markdown, no bullet characters inside strings
- No hedging filler; be specific where numbers exist in the input
"""

_EXECUTIVE_SUMMARY_MAX_WORDS = 10
_EXECUTIVE_SUMMARY_POINTER_COUNT = 5

# Colored variant: each pointer is a full sentence with a neutral lead clause,
# then a single /green or /red marker before the emphasized (good/bad) clause.
_EXECUTIVE_SUMMARY_COLORED_SYSTEM_PROMPT = """You are a CFO-facing executive brief writer.

You receive main business insights for ONE reporting window (monthly OR weekly),
optionally scoped to a single theme (a KPI group). Synthesize them into ONE
cross-cutting brief for that window only.

Return ONLY valid JSON with exactly this shape:
{"pointers": ["...", "...", "...", "...", "..."]}

Each pointer is ONE sentence with TWO parts:
1. A neutral factual lead (plain, no color).
2. Then exactly ONE marker — /green or /red — immediately before the emphasized
   clause that should be color-highlighted on the dashboard.
   - Use /red when the emphasized clause is bad news, a risk, or a negative trend.
   - Use /green when it is good news, an improvement, or a positive trend.

Example pointers (note the single inline marker):
"$9.94M pipeline is really $1.71M — only 17% moved a stage. /red 83% not moving, up from 79% last week."
"$390K OEM funding sits untapped across 11 deals. /green A closing lever, ready to act on."

Rules:
- Exactly 5 strings in the pointers array (no more, no fewer).
- Exactly ONE /green or /red marker per pointer, before the emphasized clause.
- Each pointer at most 30 words. Plain business English; no markdown, no bullet chars.
- Be specific with numbers where the input provides them.
- Cover the most important themes across ALL insights in this window/scope.
"""

_EXECUTIVE_SUMMARY_COLORED_MAX_WORDS = 30
_COLOR_MARKERS = ("/green", "/red")


def _normalize_executive_pointers_colored(raw: Any) -> list[str]:
    """Preserve the inline /green or /red marker; cap words; pad to five."""
    if not isinstance(raw, list):
        raise ValueError("Executive summary JSON must include a pointers array")
    cleaned: list[str] = []
    for p in raw:
        s = str(p).strip()
        if not s:
            continue
        s = _limit_words(s, _EXECUTIVE_SUMMARY_COLORED_MAX_WORDS)
        # Guarantee a marker so the frontend always has something to color.
        if not any(m in s for m in _COLOR_MARKERS):
            s = f"{s} /red"
        cleaned.append(s)
    while len(cleaned) < _EXECUTIVE_SUMMARY_POINTER_COUNT:
        cleaned.append("No additional cross-cutting theme identified. /red")
    return cleaned[:_EXECUTIVE_SUMMARY_POINTER_COUNT]


# Group insight: when a KPI card is clicked, synthesize ONE combined insight that
# incorporates ALL of the group's insights, plus one recommended action.
# Heading is wrapped in /h ... /h markers.
_GROUP_HIGHLIGHT_SYSTEM_PROMPT = """You are a CFO-facing analyst.

You receive ALL business insights for ONE KPI group (a single dashboard card).
Synthesize them into ONE combined insight that incorporates the key points from
EVERY insight provided. Do NOT pick just one and ignore the rest — weave the most
important facts and numbers from all of them into a single coherent story.

Return ONLY valid JSON with exactly this shape:
{"heading": "...", "description": "...", "action": "..."}

- heading: a short punchy title for the combined group story, at most 12 words.
  Plain text, no markdown, no bullet characters, no /h markers (added later).
- description: 2-4 plain sentences that combine the most important points across
  ALL the insights; be specific with the numbers present in the input.
- action: ONE concrete, actionable recommendation for this group as a whole.
"""


def _normalize_group_highlight(raw: Any) -> dict[str, Any] | None:
    """Wrap the chosen heading in /h markers; return best_insight + action."""
    if not isinstance(raw, dict):
        return None
    heading = str(raw.get("heading", "")).strip()
    description = str(raw.get("description", "")).strip()
    action = str(raw.get("action", "")).strip()
    if not any([heading, description, action]):
        return None
    return {
        "best_insight": {
            "heading": f"/h {heading} /h" if heading else "",
            "description": description,
        },
        "recommended_action": action,
    }


def _limit_words(text: str, max_words: int) -> str:
    words = [w for w in (text or "").split() if w]
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def _normalize_executive_pointers(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("Executive summary JSON must include a pointers array")
    cleaned = [
        _limit_words(str(p).strip(), _EXECUTIVE_SUMMARY_MAX_WORDS)
        for p in raw
        if str(p).strip()
    ]
    while len(cleaned) < _EXECUTIVE_SUMMARY_POINTER_COUNT:
        cleaned.append("No additional cross-cutting theme identified.")
    return cleaned[:_EXECUTIVE_SUMMARY_POINTER_COUNT]


# ─── REFINE INSIGHT SUMMARY PROMPT (CFO bullet style) ────────────────────────────

_REFINE_SUMMARY_SYSTEM_PROMPT = """You are a data analyst writing insight summaries for a CFO who has 10 seconds to read.

FORMAT RULES — follow exactly, no exceptions:
1. Output ONLY bullets. No intro line, no headers, no trailing commentary.
2. Every bullet starts with • followed by exactly one space.
3. Bold every number, percentage, pp value, dollar amount, and date using **value**.
4. Each bullet is 10–12 words maximum. If a thought exceeds 12 words, split into two bullets.
5. Separate bullets with a single newline only.
6. Dimension context (e.g. business unit, segment) appears ONCE — in the first bullet only. Never repeat it.

CONTENT RULES — follow exactly, no exceptions:
1. State only: metric name + from/to values (or vs. baseline) + dimension slice (first bullet only).
2. FORBIDDEN: causal language of any kind — no "driven by", "due to", "linked to", "suggesting", "pointing to", "compounding", "reflecting", "mask", "because", "as a result".
3. FORBIDDEN: WHY reasoning, driver attribution, or interpretive commentary of any kind.
4. Signal data only. If a value has no comparison point (from/to or vs. average), omit it.
5. If multiple data points refer to the SAME KPI/metric, COMBINE them into a single bullet (e.g. "Issue rate fell from **41%** to **28%**, **13 pp** drop"). Never have two separate bullets about the same metric.

REFERENCE OUTPUT (match this style exactly):
• Issue rate fell to **26%**, **19 pp** below rolling average — week ending **May 9, 2026**
• Demo rate fell to **55%**, **13 pp** below baseline
• Avg ticket size rose to **$20,233**, **18%** above rolling average
• Cost per demo rose **34%** week-over-week to **$543**
• Decline spanned all **9** branches, all lead source groups, all retail vendors

Return ONLY the bullets. No JSON wrapper. No commentary."""

_REFINE_SUMMARY_USER_PROMPT_TEMPLATE = """Write an insight summary for the following data. Follow the system prompt rules exactly.

Title: {title}
KPI: {kpi}
Dimension: {dimension_name} — {dimension_value}
Severity: {severity}

Current problem_statement:
{insight}

Current insight_summary (rewrite this into proper bullets):
{insight_summary}

Current why (for context only — do NOT include causal language):
{why}

Current impact (for context only):
{impact_insight}
"""


_RECOMMENDED_ACTIONS_SYSTEM_PROMPT = """You are a senior business advisor. Turn the insight pack into exactly three next steps a VP would actually take.

You will receive: Title, primary metric name, dimension context, the main "what" insight, the "why" narrative, insight summary bullets, why summary bullets, and impact bullets.

Output format (mandatory):
- Return exactly three lines of plain text — no JSON, no markdown fences, no preamble.
- Do NOT use any formatting such as **bold**, *italic*, or any special markup. Output plain text only.
- Each line must start with the Unicode bullet • followed by one space, then one complete action (full sentence or crisp clause ending naturally).
- Do not use commas to separate actions (one action per line only). Do not use ¶ or `\\r`.

Quality bar:
- Each action must name something from the pack (named metric, slice, driver, or risk) when the text allows — avoid generic platitudes.
- Never output the word KPI; use the primary metric name from the pack and concrete labels from the text.
- Use readable dimension/metric names — no raw underscore tokens like ``Branch_Name``.
- Lead with a strong verb (Realign, Prioritize, Mobilize, Commission, Shore up, Accelerate review of, …).
- Actions must feel proportionate and credible for a business leader (no hype, no invented numbers).
- Keep each line roughly under 220 characters when possible.
"""

# Dimensional main insights: only these ``signal_log.dimension`` values; one group per
# (dimension, dimension_value) — **all KPIs** on that slice are grouped together (not by KPI).
_DEFAULT_DIMENSIONAL_SLICES = ("Division", "Market_Type", "Lead_Source_Group")


def _cluster_uses_dimensional_narrative_mode(cluster: SignalCluster) -> bool:
    """Dimensional and KPI-rollup clusters share period-window WHY filtering and larger budgets."""
    return (cluster.cluster_type or "").lower() in ("dimensional", "kpi_rollup")


def _narrative_system_prompt_for_cluster(cluster: SignalCluster) -> str:
    ct = (cluster.cluster_type or "").lower()
    if ct == "kpi_rollup":
        return _ONE_SHOT_KPI_ROLLUP_MAIN_SYSTEM
    if ct == "dimensional":
        return _ONE_SHOT_DIMENSIONAL_MAIN_SYSTEM
    return _NARRATIVE_SYSTEM_PROMPT


def _kpi_rollup_slice_summary(signals: list[Signal], *, max_items: int = 16) -> str:
    """Human-readable list of dimension slices for KPI rollup cluster metadata."""
    labels: list[str] = []
    seen: set[str] = set()
    for s in signals:
        dim = (s.dimension or "").strip()
        dv = (s.dimension_value or "").strip()
        if not dim and not dv:
            continue
        label = f"{dim} — {dv}" if dim and dv else (dim or dv)
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
    labels.sort(key=str.lower)
    if not labels:
        return "Portfolio"
    if len(labels) <= max_items:
        return ", ".join(labels)
    head = ", ".join(labels[:max_items])
    return f"{head} (+{len(labels) - max_items} more slices)"


def _kpi_rollup_portal_format_user_block() -> str:
    """Remind the model of the exact dimensional main_insights column shapes."""
    return (
        "\n\n──────────────────────── PORTAL FORMAT (generate-dimensional parity) ────────────────────────\n"
        "Persisted columns: insight, why, insight_summary, why_insight_summary, impact_insight.\n"
        "- why: causal-bridge intro, then 2-3 <sub>**Theme — Observation**</sub> blocks with • bullets underneath.\n"
        "- insight_summary: • bullet lines separated by \\n (not sentences).\n"
        "- why_insight_summary: • bullet lines separated by \\n (root causes only).\n"
        "- impact_summary → impact_insight: • bullet lines.\n"
        "- Bold every number, %, $, date, dimension slice, and KPI name with **…**.\n"
    )


def _kpi_rollup_cross_dimension_block(signals: list[Signal]) -> str:
    dims = sorted(
        {(s.dimension or "").strip() for s in signals if (s.dimension or "").strip()},
        key=str.lower,
    )
    if len(dims) < 2:
        return ""
    return (
        "\n\n──────────────────────── KPI ROLLUP — CROSS-DIMENSION COVERAGE ────────────────────────\n"
        f"Analytical dimensions represented in SIGNAL ROWS: {', '.join(dims)}.\n"
        "Cover each material slice and driver in why / why_insight_summary — do not narrate only "
        "the first dimension listed. Use Portal plain-English dimension names (no underscores).\n"
    )


def _resolve_kpi_rollup_period_window(
    why_rows: list[WhyRow],
) -> tuple[date, date, str]:
    """Prefer WHY period bounds; else Portal default weekly window."""
    from .portal_period import (
        PORTAL_WEEKLY_DEFAULT_END,
        PORTAL_WEEKLY_DEFAULT_LABEL,
        PORTAL_WEEKLY_DEFAULT_START,
    )

    starts = [w.period_start for w in why_rows if w.period_start is not None]
    ends = [w.period_end for w in why_rows if w.period_end is not None]
    if starts and ends:
        p_start, p_end = min(starts), max(ends)
        return p_start, p_end, _period_bucket_label(p_start, p_end)
    return (
        PORTAL_WEEKLY_DEFAULT_START,
        PORTAL_WEEKLY_DEFAULT_END,
        PORTAL_WEEKLY_DEFAULT_LABEL,
    )


def _group_kpi_rollup_why_buckets(
    why_rows: list[WhyRow],
) -> dict[tuple[str, date, date], list[WhyRow]]:
    """Bucket WHY rows by ``(kpi_name lower, period_start, period_end)``."""
    buckets: dict[tuple[str, date, date], list[WhyRow]] = {}
    for w in why_rows:
        kpi = (w.kpi_name or "").strip()
        if not kpi or w.period_start is None or w.period_end is None:
            continue
        key = (kpi.lower(), w.period_start, w.period_end)
        buckets.setdefault(key, []).append(w)
    return buckets


def _signals_for_kpi_name(
    signals_by_kpi: dict[str, list[Signal]], kpi_lower: str
) -> tuple[str, list[Signal]]:
    for k, sigs in signals_by_kpi.items():
        if k.lower() == kpi_lower:
            return k, sigs
    return "", []


class InsightEngine:
    def __init__(
        self,
        store: ResultStore,
        *,
        main_insights_llm: MainInsightsNarrativeModel = MainInsightsNarrativeModel.azure_default,
    ):
        self.store = store
        self._main_insights_llm = main_insights_llm
        settings = get_llm_settings()
        self._temperature = settings.PLATFORM_OPENAI__TEMPERATURE
        self._llm_max_attempts = max(2, min(32, int(settings.INSIGHTS_LLM_MAX_ATTEMPTS)))
        self._llm_rate_base = float(settings.INSIGHTS_LLM_RATE_LIMIT_BASE_WAIT)
        self._llm_rate_max = float(settings.INSIGHTS_LLM_RATE_LIMIT_MAX_WAIT)
        self._main_insights_llm_concurrency = max(
            1, min(32, int(settings.INSIGHTS_MAIN_INSIGHTS_LLM_CONCURRENCY))
        )
        _raw_plog = (settings.INSIGHTS_PROMPT_LOG_DIR or "").strip()
        self._prompt_log_dir: Optional[Path] = (
            Path(_raw_plog).expanduser()
            if _raw_plog
            else (_INSIGHT_ENGINE_REPO_ROOT / "prompt_logs")
        )

        self._openai_async: Optional[AsyncAzureOpenAI] = None
        self._cohere_api_key: str = ""
        self._cohere_endpoint: str = ""
        self._cohere_api_version: str = "2024-05-01-preview"
        self._llm_provider: Literal["openai", "cohere_azure"] = "openai"
        self._model = settings.PLATFORM_OPENAI__DEPLOYMENT

        if main_insights_llm == MainInsightsNarrativeModel.azure_default:
            self._openai_async = AsyncAzureOpenAI(
                api_key=settings.PLATFORM_OPENAI__API_KEY,
                api_version=settings.PLATFORM_OPENAI__API_VERSION,
                azure_endpoint=settings.PLATFORM_OPENAI__ENDPOINT,
                max_retries=0,
            )
        elif main_insights_llm == MainInsightsNarrativeModel.azure_gpt54_mini:
            ak = settings.INSIGHTS_AZURE_GPT54_MINI__API_KEY.strip()
            ep = settings.INSIGHTS_AZURE_GPT54_MINI__ENDPOINT.strip()
            dep = settings.INSIGHTS_AZURE_GPT54_MINI__DEPLOYMENT.strip()
            if not ak or not ep or not dep:
                raise ValueError(
                    "azure_gpt54_mini requires INSIGHTS_AZURE_GPT54_MINI__API_KEY, "
                    "INSIGHTS_AZURE_GPT54_MINI__ENDPOINT, and INSIGHTS_AZURE_GPT54_MINI__DEPLOYMENT."
                )
            self._openai_async = AsyncAzureOpenAI(
                api_key=ak,
                api_version=settings.INSIGHTS_AZURE_GPT54_MINI__API_VERSION,
                azure_endpoint=ep,
                max_retries=0,
            )
            self._model = dep
        elif main_insights_llm == MainInsightsNarrativeModel.cohere_command_a_plus:
            ak = settings.INSIGHTS_COHERE_AZURE__API_KEY.strip()
            ep = (settings.INSIGHTS_COHERE_AZURE__ENDPOINT or "").strip().rstrip("/")
            ver = (
                settings.INSIGHTS_COHERE_AZURE__API_VERSION or "2024-05-01-preview"
            ).strip()
            dep = (
                settings.INSIGHTS_COHERE_AZURE__MODEL or "Cohere-command-a-plus-05-2026"
            ).strip()
            if not ak or not ep:
                raise ValueError(
                    "cohere_command_a_plus requires INSIGHTS_COHERE_AZURE__API_KEY and "
                    "INSIGHTS_COHERE_AZURE__ENDPOINT."
                )
            self._cohere_api_key = ak
            self._cohere_endpoint = ep
            self._cohere_api_version = ver
            self._model = dep
            self._llm_provider = "cohere_azure"
        else:
            raise ValueError(f"Unsupported main_insights_llm: {main_insights_llm!r}")

        if self._llm_provider == "openai" and self._openai_async is None:
            raise RuntimeError("OpenAI client not initialized")

    async def _call_gpt4o(
        self,
        system_prompt: str,
        user_prompt: str,
        context: str = "LLM",
        *,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        prompt_log_category: Optional[PromptLogCategory] = None,
        prompt_log_subfolder: Optional[str] = None,
    ) -> str:
        """Call the configured chat model (Azure OpenAI or Anthropic Foundry) with back-off on transient errors."""
        temp = self._temperature if temperature is None else float(temperature)
        last_exc: Optional[BaseException] = None
        effective_max_tokens = (
            max_tokens
            if max_tokens != 4096
            else _narrative_max_output_tokens(self._llm_provider)
        )
        for attempt in range(self._llm_max_attempts):
            try:
                if (
                    self._prompt_log_dir is not None
                    and prompt_log_category is not None
                    and attempt == 0
                ):
                    try:
                        _write_prompt_log_txt(
                            self._prompt_log_dir,
                            prompt_log_category,
                            context,
                            system_prompt,
                            user_prompt,
                            subfolder=prompt_log_subfolder,
                        )
                    except Exception:
                        logger.warning(
                            "INSIGHTS_PROMPT_LOG_DIR write failed (context=%s)",
                            context,
                            exc_info=True,
                        )
                t_llm0 = time.perf_counter()
                if self._llm_provider == "cohere_azure":
                    cohere_max = effective_max_tokens
                    request_body: dict[str, Any] = {
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": temp,
                        "max_tokens": cohere_max,
                        "max_completion_tokens": cohere_max,
                    }
                    thinking = _cohere_thinking_payload()
                    if thinking is not None:
                        request_body["thinking"] = thinking
                    cohere_timeout = max(180.0, min(900.0, cohere_max / 40.0))
                    async with httpx.AsyncClient(timeout=cohere_timeout) as client:
                        resp = await client.post(
                            f"{self._cohere_endpoint}/models/chat/completions",
                            params={"api-version": self._cohere_api_version},
                            headers={
                                "api-key": self._cohere_api_key,
                                "Content-Type": "application/json",
                            },
                            json=request_body,
                        )
                    resp.raise_for_status()
                    data = resp.json()
                    elapsed = time.perf_counter() - t_llm0
                    inp_t, out_t, tot_t = _chat_completion_usage_from_json(data)
                    logger.info(
                        "LLM usage | provider=cohere_azure model=%s context=%s elapsed_s=%.3f "
                        "input_tokens=%s output_tokens=%s total_tokens=%s max_tokens=%s attempt=%d/%d",
                        self._model,
                        context,
                        elapsed,
                        inp_t,
                        out_t,
                        tot_t,
                        cohere_max,
                        attempt + 1,
                        self._llm_max_attempts,
                    )
                    try:
                        _append_llm_metrics_jsonl(
                            self._prompt_log_dir,
                            provider="cohere_azure",
                            model=self._model,
                            context=context,
                            prompt_log_category=prompt_log_category,
                            prompt_log_subfolder=prompt_log_subfolder,
                            elapsed_seconds=elapsed,
                            input_tokens=inp_t,
                            output_tokens=out_t,
                            total_tokens=tot_t,
                            attempt_index=attempt,
                        )
                    except Exception:
                        logger.debug("llm_metrics.jsonl append failed", exc_info=True)
                    choices = data.get("choices") or []
                    if not choices:
                        raise RuntimeError("Cohere Azure returned no choices")
                    choice = choices[0]
                    msg = choice.get("message") or {}
                    out = _extract_cohere_assistant_text(msg, choice)
                    if not out:
                        finish = (choice.get("finish_reason") or "").strip()
                        raise RuntimeError(
                            "Cohere Azure returned empty message content"
                            + (f" (finish_reason={finish!r})" if finish else "")
                        )
                    return out
                assert self._openai_async is not None
                response = await self._openai_async.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temp,
                    max_tokens=max_tokens,
                )
                elapsed = time.perf_counter() - t_llm0
                inp_t, out_t, tot_t = _openai_usage_tokens(response)
                logger.info(
                    "LLM usage | provider=azure_openai model=%s context=%s elapsed_s=%.3f "
                    "input_tokens=%s output_tokens=%s total_tokens=%s attempt=%d/%d",
                    self._model,
                    context,
                    elapsed,
                    inp_t,
                    out_t,
                    tot_t,
                    attempt + 1,
                    self._llm_max_attempts,
                )
                try:
                    _append_llm_metrics_jsonl(
                        self._prompt_log_dir,
                        provider="azure_openai",
                        model=self._model,
                        context=context,
                        prompt_log_category=prompt_log_category,
                        prompt_log_subfolder=prompt_log_subfolder,
                        elapsed_seconds=elapsed,
                        input_tokens=inp_t,
                        output_tokens=out_t,
                        total_tokens=tot_t,
                        attempt_index=attempt,
                    )
                except Exception:
                    logger.debug("llm_metrics.jsonl append failed", exc_info=True)
                return response.choices[0].message.content.strip()
            except Exception as exc:
                last_exc = exc
                if not _is_transient_llm_error(exc) or attempt >= self._llm_max_attempts - 1:
                    logger.error(f"[{context}] LLM call failed: {exc}")
                    raise
                ra = _retry_after_from_openai_exc(exc)
                if ra is not None:
                    wait = min(self._llm_rate_max, max(1.0, ra))
                else:
                    exp = self._llm_rate_base * (2**attempt)
                    wait = min(self._llm_rate_max, exp + random.uniform(0.25, 1.25))
                logger.warning(
                    "[%s] Transient LLM error (%s); attempt %d/%d — sleeping %.1fs",
                    context,
                    type(exc).__name__,
                    attempt + 1,
                    self._llm_max_attempts,
                    wait,
                )
                await asyncio.sleep(wait)
        if last_exc:
            raise last_exc
        raise RuntimeError("_call_gpt4o: exhausted without exception")

    async def _llm_reformat_body_markup_only(
        self,
        raw: str,
        *,
        dimension_name: Optional[str],
        dimension_value: Optional[str],
    ) -> str:
        """Use the configured narrative model to add **…** emphasis; fall back to rules if prose changes."""
        text_in = (raw or "").replace("\r\n", "\n")
        if not text_in.strip():
            return text_in
        if len(text_in) > 28000:
            logger.warning(
                "markup_format: input %s chars > 28000; using rules-only fallback",
                len(text_in),
            )
            return reformat_body_markup_only(
                text_in, dimension_name=dimension_name, dimension_value=dimension_value
            )
        dn = (dimension_name or "").strip() or "(none)"
        dv = (dimension_value or "").strip() or "(none)"
        user = (
            f"dimension_name: {dn}\n"
            f"dimension_value: {dv}\n\n"
            "TEXT:\n"
            f"{text_in}\n"
        )
        est = max(1024, min(8192, len(text_in) // 2 + 900))
        try:
            out_raw = await self._call_gpt4o(
                _MARKUP_FORMAT_SYSTEM_PROMPT,
                user,
                context="markup_format",
                max_tokens=est,
                temperature=0.0,
                prompt_log_category="main_insight",
                prompt_log_subfolder="markup_format",
            )
        except Exception as exc:
            logger.warning("LLM markup format failed; using rules fallback: %s", exc)
            return reformat_body_markup_only(
                text_in, dimension_name=dimension_name, dimension_value=dimension_value
            )
        out = repair_broken_bold_fragments(InsightEngine._strip_code_fence(out_raw))
        if not out.strip():
            return reformat_body_markup_only(
                text_in, dimension_name=dimension_name, dimension_value=dimension_value
            )
        baseline = repair_broken_bold_fragments(text_in)
        if normalize_markup_plain_text(out) != normalize_markup_plain_text(baseline):
            logger.warning(
                "LLM markup changed plain text; using rules fallback (markup_format)"
            )
            return reformat_body_markup_only(
                text_in, dimension_name=dimension_name, dimension_value=dimension_value
            )
        return repair_broken_bold_fragments(out)

    @staticmethod
    def _alpha_number(val: Optional[float]) -> str:
        """Compact number for dedup keys and non-KPI-level fields (keeps extra precision)."""
        if val is None:
            return "n/a"
        s = f"{float(val):.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    @staticmethod
    def _iso_date(d: object) -> str | None:
        if d is None:
            return None
        if isinstance(d, (date, datetime)):
            return d.isoformat()
        return str(d)

    @staticmethod
    def _why_row_dedup_key(w: WhyRow) -> str:
        return str(w.why_id or "").strip() or (
            f"{w.signal_id}|{w.dep_kpi_name}|{w.dep_kpi_label}|{w.dimension_name}|{w.dimension_value}|"
            f"{InsightEngine._alpha_number(w.change_pct)}"
        )

    def _dedupe_why_rows(self, rows: list[WhyRow]) -> list[WhyRow]:
        out: list[WhyRow] = []
        seen: set[str] = set()
        for w in rows:
            k = self._why_row_dedup_key(w)
            if k in seen:
                continue
            seen.add(k)
            out.append(w)
        return out

    def _why_row_to_record(self, w: WhyRow) -> dict[str, Any]:
        return {
            "why_id": str(w.why_id) if w.why_id else None,
            "signal_id": str(w.signal_id),
            "kpi_name": w.kpi_name,
            "dimension_name": w.dimension_name,
            "dimension_value": w.dimension_value,
            "dep_kpi_name": w.dep_kpi_name,
            "dep_kpi_label": w.dep_kpi_label,
            "change_pct": float(w.change_pct) if w.change_pct is not None else None,
            "current_value": float(w.current_value) if w.current_value is not None else None,
            "prev_value": float(w.prev_value) if w.prev_value is not None else None,
            "period": w.period,
            "period_start": self._iso_date(w.period_start),
            "period_end": self._iso_date(w.period_end),
            "rationale": w.rationale or "",
        }

    def _why_rows_inventory_json(self, rows: list[WhyRow]) -> str:
        payload = {
            "version": 1,
            "row_count": len(rows),
            "rows": [self._why_row_to_record(w) for w in rows],
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _parse_date_field(val: object) -> date | None:
        if val is None or val == "":
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        s = str(val).strip()
        try:
            return date.fromisoformat(s[:10])
        except Exception:
            return None

    def _dict_to_why_row(self, r: dict[str, Any], *, run_ts: datetime) -> WhyRow | None:
        try:
            sig = str(r.get("signal_id") or "").strip()
            if not sig:
                return None
            wid = r.get("why_id")
            cp = r.get("change_pct")
            cv = r.get("current_value")
            pv = r.get("prev_value")
            return WhyRow(
                why_id=str(wid) if wid else None,
                signal_id=sig,
                run_timestamp=run_ts,
                kpi_name=str(r.get("kpi_name") or ""),
                dimension_name=str(r.get("dimension_name") or ""),
                dimension_value=str(r.get("dimension_value") or ""),
                signal_name="",
                dep_kpi_name=str(r.get("dep_kpi_name") or "") or None,
                dep_kpi_label=str(r.get("dep_kpi_label") or "") or None,
                rationale=str(r.get("rationale") or ""),
                current_value=float(cv) if cv is not None else None,
                prev_value=float(pv) if pv is not None else None,
                change_pct=float(cp) if cp is not None else None,
                period=str(r.get("period") or ""),
                period_start=self._parse_date_field(r.get("period_start")),
                period_end=self._parse_date_field(r.get("period_end")),
            )
        except Exception:
            return None

    def _why_map_from_inventory_json(
        self, raw: Optional[str], run_ts: datetime
    ) -> dict[str, list[WhyRow]] | None:
        """Parse cluster ``why_inventory_json`` into the same shape as ``get_whys_for_signals``.

        When present, main-insight narratives use this snapshot so WHY context matches the
        grouped run (and stays stable if ``why_results`` changes later). Returns ``None`` to fall
        back to DB-backed ``why_map``.
        """
        if not (raw or "").strip():
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Main insights | why_inventory_json JSON invalid; using why_results | run_ts=%s",
                run_ts,
            )
            return None
        rows = obj.get("rows") if isinstance(obj, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        out: dict[str, list[WhyRow]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            w = self._dict_to_why_row(r, run_ts=run_ts)
            if w:
                out.setdefault(w.signal_id, []).append(w)
        return out if out else None

    async def generate_standard_main_insights(
        self,
        run_timestamp: Optional[datetime] = None,
        all_timestamps: bool = False,
        kpi_names: Optional[list[str]] = None,
    ) -> tuple[int, Optional[datetime], list[str]]:
        """One LLM main insight per (KPI × analytical dimension)."""
        t0 = time.perf_counter()
        logger.info(
            "Standard main insights (one-shot) | all_timestamps=%s run_timestamp=%s kpi_filter=%s",
            all_timestamps,
            run_timestamp,
            kpi_names,
        )
        if all_timestamps:
            signals = await self.store.get_all_signals()
            ts = max((s.detected_at for s in signals), default=None)
        elif run_timestamp is not None:
            signals = await self.store.get_signals_by_timestamp(run_timestamp)
            ts = run_timestamp
        else:
            signals = await self.store.get_signals_latest_per_kpi_dimension()
            ts = max((s.detected_at for s in signals), default=None)

        if not signals or ts is None:
            logger.info("Standard main insights: no signals found.")
            return 0, None, []

        allow = {(x or "").strip().lower() for x in (kpi_names or []) if (x or "").strip()}
        if allow:
            signals = [s for s in signals if (s.kpi_name or "").strip().lower() in allow]
            ts = max((s.detected_at for s in signals), default=None)
            if not signals or ts is None:
                return 0, None, []

        signal_ids = [s.signal_id for s in signals]
        why_rows_all = await self.store.get_whys_for_signals(signal_ids)
        why_map: dict[str, list[WhyRow]] = {}
        for w in why_rows_all:
            why_map.setdefault(w.signal_id, []).append(w)

        groups: dict[tuple[str, str], list[Signal]] = {}
        for s in signals:
            groups.setdefault((s.kpi_name, s.dimension), []).append(s)

        skipped: list[str] = []
        clusters: list[SignalCluster] = []
        why_lim_std = _narrative_why_raw_max_chars(dimensional=False)
        for (kpi_name, dim_name), sigs in groups.items():
            sids = list(dict.fromkeys(x.signal_id for x in sigs if x.signal_id))
            dim_values = sorted(
                {(x.dimension_value or "").strip() for x in sigs if (x.dimension_value or "").strip()}
            )
            dv_joined = ", ".join(dim_values)[:4000]
            why_rows_g: list[WhyRow] = []
            for sid in sids:
                why_rows_g.extend(why_map.get(sid) or [])
            why_rows_g = self._dedupe_why_rows(why_rows_g)
            why_inv = self._why_rows_inventory_json(why_rows_g) if why_rows_g else None
            why_line_strs = _why_lines_for_narrative(
                sids, why_map, rationale_max=None, max_per_signal=None, max_total_lines=None
            )
            why_text = "\n".join(why_line_strs) if why_line_strs else ""
            if (
                why_text
                and len(why_text) <= why_lim_std
                and why_rows_g
            ):
                cov = _distinct_dep_driver_preamble(why_rows_g)
                if cov:
                    why_text = cov + "\n\n" + why_text
            if why_text and len(why_text) > why_lim_std:
                skipped.append(
                    f"standard: {kpi_name} / {dim_name} "
                    f"(why_text {len(why_text)} chars > {why_lim_std}; skipped)"
                )
                continue
            clusters.append(
                SignalCluster(
                    cluster_id=str(uuid4()),
                    run_timestamp=ts,
                    kpi_name=kpi_name,
                    dimension_name=dim_name,
                    dimension_value=dv_joined,
                    period="Mixed",
                    signal_ids=",".join(sids),
                    cluster_type="alpha",
                    why_inventory_json=why_inv,
                )
            )

        n_main = 0
        if clusters:
            n_main = await self._persist_main_insights_from_clusters(
                clusters, run_ts=ts, cluster_type_label="alpha"
            )
        logger.info(
            "Standard main insights complete | groups=%s persisted=%s skipped=%s elapsed=%.1fs",
            len(clusters),
            n_main,
            len(skipped),
            time.perf_counter() - t0,
        )
        return n_main, ts, skipped


    async def cluster_signals_dimensional(
        self,
        run_timestamp: Optional[datetime] = None,
        target_dimensions: Optional[list[str]] = None,
        all_timestamps: bool = False,
    ) -> tuple[int, Optional[datetime], list[str]]:
        """Dimensional main insights for fixed slice dimensions only (cross-KPI by slice).

        Buckets are built in **PostgreSQL** via ``json_agg(t) FROM (SELECT <column subset> ...) t`` for
        both ``signal_log`` and ``why_results`` (join on ``signal_id``), grouped by slice and
        ``period_start`` / ``period_end``.

        Default dimensions: **Division**, **Market_Type**, **Lead_Source_Group** (must match
        ``signal_log.dimension``). Each ``slice × period bucket`` becomes **one** ``SignalCluster`` →
        one main insight. Only signals with WHY rows in that window are included (via the join).

        ``SignalCluster.kpi_name`` holds a comma-separated list of distinct KPI names for signals
        in that bucket. Override ``target_dimensions`` for a different allow-list.

        Returns ``(main_insights_written, run_timestamp, skipped_descriptions)``. Persists **main_insights**
        only. Skips buckets when WHY text exceeds the raw character budget (no chunk-merge).
        """
        if target_dimensions is None:
            target_dimensions = list(_DEFAULT_DIMENSIONAL_SLICES)

        # Flatten comma-separated values the user may pass via Swagger
        flat_dims: list[str] = []
        for d in target_dimensions:
            flat_dims.extend([x.strip() for x in d.split(",") if x.strip()])
        target_dims_lower = [d.lower() for d in flat_dims]
        dim_lower_to_display = {d.lower(): d for d in flat_dims}

        if all_timestamps:
            ts = await self.store.get_max_signal_detected_at()
        else:
            ts = run_timestamp or await self.store.get_latest_signal_timestamp()
        if not ts:
            logger.info("No signals found to cluster (dimensional).")
            return 0, None, []

        buckets = await self.store.fetch_dimensional_buckets_json_agg(
            target_dimensions_lower=target_dims_lower,
            all_timestamps=all_timestamps,
            batch_ts=ts,
        )
        if not buckets:
            logger.info(
                "Dimensional clustering | no buckets from SQL (json_agg): "
                "no matching WHY rows with period_start/period_end for target dimensions %s",
                flat_dims,
            )
            return 0, ts, []

        n_why_sql = sum(len(b.get("why_results_json") or []) for b in buckets)
        logger.info(
            "Dimensional clustering | SQL json_agg | buckets=%s total_why_rows=%s",
            len(buckets),
            n_why_sql,
        )

        results = []
        skipped_dimensional: list[str] = []
        why_lim_dim = _narrative_why_raw_max_chars(dimensional=True)
        for b in buckets:
            p_start = self._parse_date_field(b.get("period_start"))
            p_end = self._parse_date_field(b.get("period_end"))
            if p_start is None or p_end is None:
                continue

            raw_dim = (b.get("dimension_name") or "").strip()
            dim_display = dim_lower_to_display.get(raw_dim.lower(), raw_dim)
            raw_dv = (b.get("dimension_value") or "").strip()

            signals_bucket: list[Signal] = []
            for row in b.get("signals_json") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    signals_bucket.append(
                        self.store.signal_from_signal_log_json_row(
                            row, default_detected_at=ts
                        )
                    )
                except Exception:
                    logger.warning("Dimensional | skip malformed signal_log JSON row", exc_info=True)
                    continue

            why_rows_cluster: list[WhyRow] = []
            for row in b.get("why_results_json") or []:
                if not isinstance(row, dict):
                    continue
                w = self._dict_to_why_row(row, run_ts=ts)
                if w:
                    # SQL JSON omits ``period_start`` / ``period_end``; attach bucket window from GROUP BY.
                    why_rows_cluster.append(
                        w.model_copy(update={"period_start": p_start, "period_end": p_end})
                    )
            why_rows_cluster = _sort_why_rows_for_narrative_driver_first(why_rows_cluster)

            sids_bucket = list(dict.fromkeys(s.signal_id for s in signals_bucket if s.signal_id))
            if not sids_bucket and why_rows_cluster:
                sids_bucket = list(
                    dict.fromkeys(w.signal_id for w in why_rows_cluster if w.signal_id)
                )
                if sids_bucket:
                    signals_bucket = await self.store.get_signals_latest_row_per_signal_ids(
                        sids_bucket
                    )
            if not sids_bucket:
                continue

            dv = _canonical_slice_dimension_value(raw_dv, signals_bucket)

            kpis_sorted = sorted(
                {s.kpi_name for s in signals_bucket if (s.kpi_name or "").strip()}
            )
            kpi_name_stored = (", ".join(kpis_sorted))[:2000] if kpis_sorted else "—"

            n_sig_lines = len(signals_bucket)
            if n_sig_lines > 400:
                logger.warning(
                    "Dimensional cluster %s — %s | %s has %d signal lines (large LLM prompt); consider narrowing data.",
                    dim_display,
                    dv[:80],
                    _period_bucket_label(p_start, p_end),
                    n_sig_lines,
                )

            why_map_bucket: dict[str, list[WhyRow]] = {}
            for w in why_rows_cluster:
                why_map_bucket.setdefault(w.signal_id, []).append(w)

            why_inv = (
                self._why_rows_inventory_json(why_rows_cluster) if why_rows_cluster else None
            )
            why_line_strs = _why_lines_for_narrative(
                sids_bucket,
                why_map_bucket,
                rationale_max=None,
                max_per_signal=None,
                max_total_lines=None,
            )
            why_text = "\n".join(why_line_strs) if why_line_strs else ""
            if (
                why_text
                and len(why_text) <= why_lim_dim
                and why_rows_cluster
            ):
                cov = _distinct_dep_driver_preamble(why_rows_cluster)
                if cov:
                    why_text = cov + "\n\n" + why_text
            if why_text and len(why_text) > why_lim_dim:
                dv_short = dv[:120]
                msg = (
                    f"dimensional: {dim_display} — {dv_short} | "
                    f"{_period_bucket_label(p_start, p_end)} "
                    f"(why_text {len(why_text)} chars > {why_lim_dim}; skipped — no chunking)"
                )
                logger.warning("Clustering skip | %s", msg)
                skipped_dimensional.append(msg)
                continue
            if not why_text:
                why_text = (
                    "(no WHY rows in why_results for these signal_ids — run the WHY sweep if you need drivers.)"
                )
            period_label = _period_bucket_label(p_start, p_end)
            results.append(
                SignalCluster(
                    cluster_id=str(uuid4()),
                    run_timestamp=ts,
                    kpi_name=kpi_name_stored,
                    dimension_name=dim_display,
                    dimension_value=dv,
                    period=period_label,
                    signal_ids=",".join(sids_bucket),
                    cluster_type="dimensional",
                    why_inventory_json=why_inv,
                    period_start=p_start,
                    period_end=p_end,
                )
            )

        logger.info(
            "Formed %d dimensional clusters (slice × WHY period bucket, cross-KPI).",
            len(results),
        )

        n_main = 0
        if results:
            n_main = await self._persist_main_insights_from_clusters(
                results,
                run_ts=ts,
                cluster_type_label="dimensional",
            )
            logger.info(
                "Dimensional pipeline | dimensional_groups=%s main_insights_persisted=%s",
                len(results),
                n_main,
            )
        if skipped_dimensional:
            logger.info(
                "Dimensional clustering skipped (no chunking) | count=%s | samples=%s",
                len(skipped_dimensional),
                skipped_dimensional[:20],
            )
        return n_main, ts, skipped_dimensional

    async def generate_kpi_rollup_main_insights(
        self,
        run_timestamp: Optional[datetime] = None,
        all_timestamps: bool = False,
        kpi_names: Optional[list[str]] = None,
    ) -> tuple[int, Optional[datetime], list[str]]:
        """One main insight per configured KPI (all dimension slices combined).

        Iterates every row in ``configkpisclientportal`` (or ``kpi_names`` filter). KPIs with
        rows in ``signal_log`` get one LLM narrative. WHY rows are optional — when present
        (with ``period_start`` / ``period_end``), one cluster is formed per period bucket;
        otherwise a single signal-based cluster is created.
        """
        t0 = time.perf_counter()
        logger.info(
            "KPI rollup main insights | all_timestamps=%s run_timestamp=%s kpi_filter=%s",
            all_timestamps,
            run_timestamp,
            kpi_names,
        )
        if all_timestamps:
            signals = await self.store.get_all_signals()
            ts = max((s.detected_at for s in signals), default=None)
        elif run_timestamp is not None:
            signals = await self.store.get_signals_by_timestamp(run_timestamp)
            ts = run_timestamp
        else:
            signals = await self.store.get_signals_latest_per_kpi_dimension()
            ts = max((s.detected_at for s in signals), default=None)

        if ts is None:
            logger.info("KPI rollup main insights: no signal_log batch timestamp.")
            return 0, None, []

        allow = {(x or "").strip().lower() for x in (kpi_names or []) if (x or "").strip()}
        if allow:
            target_kpis = sorted(
                {x for x in (kpi_names or []) if (x or "").strip()},
                key=str.lower,
            )
        else:
            target_kpis = list(await self.store.list_config_kpi_names())

        if not target_kpis:
            logger.info("KPI rollup main insights: no KPIs in configkpisclientportal.")
            return 0, ts, []

        if allow:
            signals = [s for s in signals if (s.kpi_name or "").strip().lower() in allow]

        sig_by_id = {s.signal_id: s for s in signals if s.signal_id}
        signal_ids = list(sig_by_id.keys())
        why_rows_all = await self.store.get_whys_for_signals(signal_ids) if signal_ids else []
        if allow:
            why_rows_all = [
                w
                for w in why_rows_all
                if (w.kpi_name or "").strip().lower() in allow
            ]

        signals_by_kpi: dict[str, list[Signal]] = {}
        for s in signals:
            kpi_key = (s.kpi_name or "").strip()
            if not kpi_key:
                continue
            signals_by_kpi.setdefault(kpi_key, []).append(s)

        skipped: list[str] = []
        clusters: list[SignalCluster] = []
        why_lim = _narrative_why_raw_max_chars(dimensional=True)

        def _append_kpi_cluster(
            *,
            kpi_stored: str,
            signals_bucket: list[Signal],
            sids_bucket: list[str],
            why_rows_cluster: list[WhyRow],
            p_start: date,
            p_end: date,
            period_label: str,
        ) -> None:
            why_map_bucket: dict[str, list[WhyRow]] = {}
            for w in why_rows_cluster:
                why_map_bucket.setdefault(w.signal_id, []).append(w)

            why_inv = (
                self._why_rows_inventory_json(why_rows_cluster) if why_rows_cluster else None
            )
            if why_rows_cluster:
                why_line_strs = _why_lines_for_narrative(
                    sids_bucket,
                    why_map_bucket,
                    rationale_max=None,
                    max_per_signal=None,
                    max_total_lines=None,
                )
                why_text = "\n".join(why_line_strs) if why_line_strs else ""
                if why_text and len(why_text) <= why_lim:
                    cov = _distinct_dep_driver_preamble(why_rows_cluster)
                    if cov:
                        why_text = cov + "\n\n" + why_text
                if why_text and len(why_text) > why_lim:
                    skipped.append(
                        f"kpi_rollup: {kpi_stored} | {period_label} "
                        f"(why_text {len(why_text)} chars > {why_lim}; skipped)"
                    )
                    return

            slice_summary = _kpi_rollup_slice_summary(signals_bucket)
            clusters.append(
                SignalCluster(
                    cluster_id=str(uuid4()),
                    run_timestamp=ts,
                    kpi_name=kpi_stored,
                    dimension_name="KPI Portfolio",
                    dimension_value=slice_summary,
                    period=period_label,
                    signal_ids=",".join(sids_bucket),
                    cluster_type="kpi_rollup",
                    why_inventory_json=why_inv,
                    period_start=p_start,
                    period_end=p_end,
                )
            )

        for kpi_key in target_kpis:
            kpi_stored, signals_bucket = _signals_for_kpi_name(
                signals_by_kpi, kpi_key.lower()
            )
            if not signals_bucket:
                skipped.append(
                    f"kpi_rollup: {kpi_key} (no signals in signal_log — run signal detection first)"
                )
                continue

            sids_bucket = list(
                dict.fromkeys(s.signal_id for s in signals_bucket if s.signal_id)
            )
            why_rows_kpi = [
                w
                for w in why_rows_all
                if w.signal_id in sids_bucket
                or (w.kpi_name or "").strip().lower() == kpi_key.lower()
            ]
            why_rows_kpi = self._dedupe_why_rows(why_rows_kpi)
            why_rows_kpi = _sort_why_rows_for_narrative_driver_first(why_rows_kpi)

            period_buckets = _group_kpi_rollup_why_buckets(why_rows_kpi)
            if period_buckets:
                for (_kpi_lower, p_start, p_end), why_rows_raw in sorted(
                    period_buckets.items(),
                    key=lambda item: (item[0][0], item[0][1], item[0][2]),
                ):
                    why_rows_cluster = self._dedupe_why_rows(why_rows_raw)
                    why_rows_cluster = _sort_why_rows_for_narrative_driver_first(
                        why_rows_cluster
                    )
                    if not why_rows_cluster:
                        continue
                    period_label = _period_bucket_label(p_start, p_end)
                    _append_kpi_cluster(
                        kpi_stored=kpi_stored or kpi_key,
                        signals_bucket=signals_bucket,
                        sids_bucket=sids_bucket,
                        why_rows_cluster=why_rows_cluster,
                        p_start=p_start,
                        p_end=p_end,
                        period_label=period_label,
                    )
            else:
                p_start, p_end, period_label = _resolve_kpi_rollup_period_window(
                    why_rows_kpi
                )
                _append_kpi_cluster(
                    kpi_stored=kpi_stored or kpi_key,
                    signals_bucket=signals_bucket,
                    sids_bucket=sids_bucket,
                    why_rows_cluster=why_rows_kpi,
                    p_start=p_start,
                    p_end=p_end,
                    period_label=period_label,
                )

        n_main = 0
        if clusters:
            n_main = await self._persist_main_insights_from_clusters(
                clusters,
                run_ts=ts,
                cluster_type_label="kpi_rollup",
            )
        logger.info(
            "KPI rollup main insights complete | target_kpis=%s clusters=%s persisted=%s skipped=%s elapsed=%.1fs",
            len(target_kpis),
            len(clusters),
            n_main,
            len(skipped),
            time.perf_counter() - t0,
        )
        return n_main, ts, skipped

    async def _narrative_json_one_shot(
        self,
        cluster: SignalCluster,
        sig_map: dict[str, Signal],
        why_map: dict[str, list[WhyRow]],
        *,
        dimensional: bool,
    ) -> Optional[dict]:
        """Single LLM call for full narrative JSON (no per-signal chunking or merge)."""
        sids = [p.strip() for p in cluster.signal_ids.split(",") if p.strip()]
        cluster_sigs = [sig_map[sid] for sid in sids if sid in sig_map]
        if not cluster_sigs:
            return None

        why_eff = why_map
        if (
            _cluster_uses_dimensional_narrative_mode(cluster)
            and cluster.period_start is not None
            and cluster.period_end is not None
        ):
            why_eff = _why_map_for_period_window(
                why_map, sids, cluster.period_start, cluster.period_end
            )

        ctx_base = "oneshot_" + (cluster.kpi_name or "cluster").replace("\n", " ")[:120]
        mt = _narrative_max_output_tokens(self._llm_provider)
        why_rows_cluster: list[WhyRow] = []
        for sid in sids:
            why_rows_cluster.extend(why_eff.get(sid) or [])
        why_rows_cluster = _sort_why_rows_for_narrative_driver_first(why_rows_cluster)
        why_json = _why_rows_json_for_prompt(why_rows_cluster)
        if why_rows_cluster:
            cov = _distinct_dep_driver_preamble(why_rows_cluster)
            prefix = (cov + "\n") if cov else ""
            candidate = prefix + why_json
            why_lim = _narrative_why_raw_max_chars(
                dimensional=_cluster_uses_dimensional_narrative_mode(cluster)
            )
            if len(candidate) > why_lim:
                logger.warning(
                    "One-shot narrative skip | cluster_id=%s | why_json_chars=%s",
                    cluster.cluster_id,
                    len(candidate),
                )
                return None
        user = _fill_narrative_user_prompt_one_shot(
            cluster=cluster,
            cluster_sigs=cluster_sigs,
            why_rows=why_rows_cluster,
            why_eff=why_eff,
        )
        system = _narrative_system_prompt_for_cluster(cluster)
        subfolder = (cluster.cluster_type or "one_shot").lower()
        raw = await self._call_gpt4o(
            system,
            user,
            context=ctx_base,
            max_tokens=mt,
            prompt_log_category="main_insight",
            prompt_log_subfolder=f"one_shot_{subfolder}",
        )
        return _parse_narrative_json(raw)

    async def _build_one_main_insight(
        self,
        cluster: SignalCluster,
        sig_map: dict[str, Signal],
        why_map: dict[str, list[WhyRow]],
        run_ts: datetime,
    ) -> Optional[MainInsight]:
        try:
            why_for_narrative = (
                self._why_map_from_inventory_json(cluster.why_inventory_json, run_ts) or why_map
            )
            if why_for_narrative is not why_map and cluster.why_inventory_json:
                n_why = sum(len(v) for v in why_for_narrative.values())
                logger.info(
                    "Main insights | cluster_id=%s using why_inventory_json for narrative "
                    "(signals_with_why=%s total_why_rows=%s)",
                    cluster.cluster_id,
                    len(why_for_narrative),
                    n_why,
                )
            data = await self._narrative_json_one_shot(
                cluster,
                sig_map,
                why_for_narrative,
                dimensional=_cluster_uses_dimensional_narrative_mode(cluster),
            )
            if not data:
                logger.info(
                    "Main insights | skipped empty narrative | cluster_id=%s",
                    cluster.cluster_id,
                )
                return None
            tags_str = _normalize_tags_for_main_insight(data.get("tags"))
            period_start, period_end = _period_bounds_for_cluster(cluster, why_for_narrative)
            ml = cluster.kpi_name or ""
            title = _polish_prose(
                _coerce_title_for_main_insight(
                    _narrative_text_field(data.get("title"), ""),
                    metric_label=ml,
                    dimension_value=cluster.dimension_value,
                ),
                metric_label=ml,
            )
            insight_problem = _narrative_text_field(data.get("problem_statement"), "")
            insight_snapshot = _narrative_text_field(data.get("signal_snapshot"), "")
            insight_raw = _narrative_text_field(data.get("insight"), "")
            if not insight_raw:
                parts = [p for p in (insight_problem, insight_snapshot) if p]
                insight_raw = "\n\n".join(parts)
            insight = _polish_prose(insight_raw, metric_label=ml)
            why = _polish_prose(_narrative_text_field(data.get("why"), ""), metric_label=ml)
            period_context = _narrative_period_context(cluster, why_for_narrative)
            insight = _ensure_period_mention(insight, period_context)
            why = _ensure_period_mention(why, period_context)
            insight_summary = _polish_prose(
                _narrative_text_or_bullets(data.get("insight_summary")), metric_label=ml
            )
            why_insight_summary = _polish_prose(
                _narrative_text_or_bullets(data.get("why_insight_summary")),
                metric_label=ml,
            )
            impact_raw = data.get("impact_insight")
            if impact_raw is None:
                impact_raw = data.get("impact_summary")
            impact_insight = _polish_prose(
                _narrative_text_or_bullets(impact_raw, max_bullets=5),
                metric_label=ml,
            )
            def _base_clean(s: str) -> str:
                return _scrub_pipeline_jargon_for_executives(
                    _strip_route_labels_for_main_insight(s)
                )

            dn = (cluster.dimension_name or "").strip() or None
            dv = (cluster.dimension_value or "").strip() or None

            def _finalize_body_field(s: str) -> str:
                return apply_dimension_highlights(
                    apply_frontend_bold_markup(
                        normalize_body_bullets_and_markdown(_base_clean(s))
                    ),
                    dimension_name=dn,
                    dimension_value=dv,
                )

            title = strip_title_markup(_base_clean(title))
            insight = _finalize_body_field(insight)
            why = normalize_why_subsection_structure(_finalize_body_field(why))
            if insight_summary:
                insight_summary = _finalize_body_field(insight_summary)
            if why_insight_summary:
                why_insight_summary = _finalize_body_field(why_insight_summary)
            if impact_insight:
                impact_insight = _finalize_body_field(impact_insight)
            return MainInsight(
                run_timestamp=run_ts,
                signal_ids=cluster.signal_ids,
                kpi_family=cluster.kpi_name,
                title=title,
                kpi=cluster.kpi_name,
                dimension_name=cluster.dimension_name,
                dimension_value=cluster.dimension_value,
                insight=insight,
                why=why,
                period=cluster.period,
                period_start=period_start,
                period_end=period_end,
                insight_summary=insight_summary,
                why_insight_summary=why_insight_summary,
                severity=data.get("severity") if isinstance(data.get("severity"), str) else None,
                tags=tags_str,
                impact_insight=impact_insight,
            )
        except Exception as e:
            logger.error(
                "Failed to generate narrative for cluster %s: %s",
                cluster.cluster_id,
                e,
            )
            return None

    async def _persist_main_insights_from_clusters(
        self,
        clusters: list[SignalCluster],
        *,
        run_ts: datetime,
        cluster_type_label: Optional[str] = None,
    ) -> int:
        """Fetch signal_log + why_results for cluster signal_ids, run narrative LLM, write main_insights.

        Used by :meth:`generate_standard_main_insights` and dimensional slice processing (in-memory
        ``SignalCluster`` groupings only; no persistence of clusters).
        """
        if not clusters:
            return 0
        ctl = cluster_type_label or "unspecified"
        referenced_ids: list[str] = []
        for cluster in clusters:
            for part in cluster.signal_ids.split(","):
                p = part.strip()
                if p:
                    referenced_ids.append(p)
        unique_ids = list(dict.fromkeys(referenced_ids))
        t_ctx = time.perf_counter()
        logger.info(
            "Main insights | fetching signal_log | source=%s clusters=%s unique_signal_ids=%s",
            ctl,
            len(clusters),
            len(unique_ids),
        )
        all_signals = await self.store.get_signals_latest_row_per_signal_ids(unique_ids)
        sig_map = {s.signal_id: s for s in all_signals}
        logger.info(
            "Main insights | signal rows loaded | resolved_signals=%s elapsed=%.1fs",
            len(sig_map),
            time.perf_counter() - t_ctx,
        )

        t_why = time.perf_counter()
        all_whys = await self.store.get_whys_for_signals(list(sig_map.keys()))
        why_map: dict[str, list[WhyRow]] = {}
        for w in all_whys:
            why_map.setdefault(w.signal_id, []).append(w)
        logger.info(
            "Main insights | why rows loaded | why_row_count=%s elapsed=%.1fs | context_total=%.1fs",
            len(all_whys),
            time.perf_counter() - t_why,
            time.perf_counter() - t_ctx,
        )

        n_clusters = len(clusters)
        logger.info(
            "Main insights | starting LLM narratives | source=%s clusters=%s | llm_concurrency=%s | "
            "run_timestamp=%s",
            ctl,
            n_clusters,
            self._main_insights_llm_concurrency,
            run_ts,
        )
        t_run = time.perf_counter()
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        progress_lock = asyncio.Lock()
        write_lock = asyncio.Lock()
        done_count: list[int] = [0]
        persisted_count: list[int] = [0]

        async def run_one(c: SignalCluster) -> Optional[MainInsight]:
            n_sig = _signal_count_cluster(c)
            async with sem:
                logger.info(
                    "Main insights | LLM call starting | cluster_id=%s | n_signals=%s | source=%s",
                    c.cluster_id,
                    n_sig,
                    ctl,
                )
                ins = await self._build_one_main_insight(c, sig_map, why_map, run_ts)
            persisted_total: int | None = None
            if ins is not None:
                async with write_lock:
                    await self.store.write_main_insights([ins])
                    persisted_count[0] += 1
                    persisted_total = persisted_count[0]
            async with progress_lock:
                done_count[0] += 1
                dn = done_count[0]
                elapsed = time.perf_counter() - t_run
                avg = elapsed / dn if dn else 0.0
                eta_s = max(0.0, avg * (n_clusters - dn))
                em, es = int(eta_s // 60), int(eta_s % 60)
                eta_part = f"eta ~{em}m{es:02d}s" if em else f"eta ~{es}s"
                persist_part = (
                    f" | rows_in_db={persisted_total}"
                    if persisted_total is not None
                    else " | rows_in_db=(unchanged)"
                )
                logger.info(
                    "Main insights progress | source=%s | done %s/%s | cluster_id=%s | n_signals=%s%s | %s",
                    ctl,
                    dn,
                    n_clusters,
                    c.cluster_id,
                    n_sig,
                    persist_part,
                    eta_part,
                )
            return ins

        merged = await asyncio.gather(*(run_one(c) for c in clusters))
        results = [x for x in merged if x is not None]

        total_s = time.perf_counter() - t_run
        logger.info(
            "Main insights | finished | source=%s | persisted=%s | clusters_in_run=%s | elapsed=%.1fs",
            ctl,
            len(results),
            n_clusters,
            total_s,
        )
        return len(results)

    async def generate_main_insights(
        self,
        run_timestamp: Optional[datetime] = None,
        cluster_type: Optional[str] = None,
    ) -> int:
        """One-shot standard main insights for one ``signal_log`` batch (``run_timestamp``).

        ``cluster_type`` is ignored (kept for API compatibility). Does not read or write cluster tables.
        """
        ts = run_timestamp or await self.store.get_latest_signal_timestamp()
        if not ts:
            logger.info(
                "Main insights | stopped: no batch timestamp (pass run_timestamp or load signal_log)"
            )
            return 0
        logger.info(
            "Main insights | standard one-shot | run_timestamp=%s cluster_type=%s (ignored)",
            ts,
            cluster_type,
        )
        n, _, _ = await self.generate_standard_main_insights(
            run_timestamp=ts, all_timestamps=False
        )
        return n

    @staticmethod
    def _clip_text(val: Optional[str], max_chars: int = 12000) -> str:
        if not val:
            return ""
        s = str(val).strip()
        if len(s) <= max_chars:
            return s
        return s[: max_chars - 1] + "…"

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        s = text.strip()
        if s.startswith("```"):
            nl = s.find("\n")
            s = s[nl + 1 :] if nl != -1 else s[3:]
            if s.rstrip().endswith("```"):
                s = s.rstrip()[:-3].strip()
        return s.strip()

    @staticmethod
    def _format_recommended_actions_for_db(raw: str) -> str:
        """Exactly three ``• `` lines for ``recommended_actions``; pad only when the model returned 1–2 actions."""
        s = InsightEngine._strip_code_fence(raw).strip()
        if not s:
            return ""
        items = _bullet_text_to_items(s)
        if len(items) == 1 and items[0].count(",") >= 2:
            items = [
                re.sub(r"^[•\-\*–—]\s*", "", p).strip()
                for p in items[0].replace(";", ",").split(",")
                if p.strip()
            ]
        items = [re.sub(r"^[•\-\*–—]\d*\.?\s*", "", x).strip() for x in items]
        items = [x for x in items if x]
        if not items:
            return ""
        items = items[:3]
        if len(items) < 3:
            pad_i = 0
            while len(items) < 3:
                items.append(_RECOMMENDED_ACTION_PAD[pad_i % len(_RECOMMENDED_ACTION_PAD)])
                pad_i += 1
        return "\n".join(f"• {x}" for x in items[:3])[:48000]

    async def reformat_stored_main_insights_markup(
        self,
        *,
        insight_id: Optional[UUID] = None,
        run_timestamp: Optional[datetime] = None,
        limit: int = 2000,
        use_llm: bool = True,
    ) -> int:
        """Re-apply display markup on narrative columns (title stays rule-stripped).

        With ``use_llm=True`` (default), each non-empty body field is sent to the configured
        narrative model with a strict “markup only” prompt; if the model changes plain text
        or errors, that field falls back to deterministic rules.
        """
        rows = await self.store.list_main_insights_for_markup_reformat(
            insight_id=insight_id,
            run_timestamp=run_timestamp,
            limit=limit,
        )
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        updated = 0
        for row in rows:
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))
            dn = row.get("dimension_name")
            dv = row.get("dimension_value")

            async def rf_body(
                raw: Optional[str],
                *,
                _dn: Optional[str] = dn,
                _dv: Optional[str] = dv,
            ) -> Optional[str]:
                if raw is None:
                    return None
                if not str(raw).strip():
                    return raw
                if use_llm:
                    async with sem:
                        return await self._llm_reformat_body_markup_only(
                            str(raw), dimension_name=_dn, dimension_value=_dv
                        )
                return reformat_body_markup_only(
                    str(raw), dimension_name=_dn, dimension_value=_dv
                )

            (
                insight_out,
                why_out,
                insight_summary_out,
                why_insight_summary_out,
                impact_out,
                ra_out,
            ) = await asyncio.gather(
                rf_body(row.get("insight") or ""),
                rf_body(row.get("why")),
                rf_body(row.get("insight_summary")),
                rf_body(row.get("why_insight_summary")),
                rf_body(row.get("impact_insight")),
                rf_body(row.get("recommended_actions")),
            )
            why_out = (
                normalize_why_subsection_structure(why_out)
                if why_out is not None
                else None
            )
            n = await self.store.update_main_insight_markup_columns(
                uid,
                title=reformat_title_markup_only(row.get("title")),
                insight=insight_out or "",
                why=why_out,
                insight_summary=insight_summary_out,
                why_insight_summary=why_insight_summary_out,
                impact_insight=impact_out,
                recommended_actions=ra_out,
            )
            updated += n
        logger.info(
            "reformat_stored_main_insights_markup updated %s row(s) (insight_id=%s run_timestamp=%s limit=%s use_llm=%s)",
            updated,
            insight_id,
            run_timestamp,
            limit,
            use_llm,
        )
        return updated

    async def summarize_main_insight_why_with_sonnet(
        self,
        *,
        run_timestamp: Optional[datetime] = None,
        insight_id: Optional[UUID] = None,
        limit: int = 2000,
    ) -> tuple[int, Optional[datetime]]:
        """Rewrite only ``why`` text for main_insights rows using the configured Sonnet model."""
        ts_used: Optional[datetime] = None
        if insight_id is not None:
            rows = await self.store.list_main_insights_for_why_summarization(
                insight_id=insight_id,
                limit=1,
            )
            ts_used = rows[0]["run_timestamp"] if rows else None
        else:
            ts_used = run_timestamp or await self.store.get_latest_main_insight_run_timestamp()
            if not ts_used:
                logger.info("No main_insights rows; skip why summarization.")
                return 0, None
            rows = await self.store.list_main_insights_for_why_summarization(
                run_timestamp=ts_used,
                limit=limit,
            )
        if not rows:
            return 0, ts_used

        updated = 0
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        for row in rows:
            why_raw = self._clip_text(row.get("why"), max_chars=28000)
            if not why_raw:
                continue
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))
            user_prompt = _WHY_SUMMARY_SONNET_USER_PROMPT_TEMPLATE.format(
                insight_title=(row.get("title") or "").strip(),
                kpi=(row.get("kpi") or "").strip(),
                dimension_name=(row.get("dimension_name") or "").strip() or "n/a",
                dimension_value=(row.get("dimension_value") or "").strip() or "n/a",
                why=why_raw,
            )
            try:
                async with sem:
                    out_raw = await self._call_gpt4o(
                        _WHY_SUMMARY_SONNET_SYSTEM_PROMPT,
                        user_prompt,
                        context=f"why_summary_{row.get('kpi') or 'row'}",
                        max_tokens=3000,
                        temperature=0.0,
                        prompt_log_category="main_insight",
                        prompt_log_subfolder="why_summary_sonnet",
                    )
                summarized = normalize_why_subsection_structure(
                    repair_broken_bold_fragments(
                        InsightEngine._strip_code_fence(out_raw)
                    ).strip()
                )
                if not summarized.strip():
                    continue
                n = await self.store.update_main_insight_why(uid, summarized)
                updated += n
            except Exception as exc:
                logger.error("WHY summarization failed for insight_id=%s: %s", uid, exc)
        logger.info(
            "summarize_main_insight_why_with_sonnet updated %s row(s) (insight_id=%s run_timestamp=%s limit=%s)",
            updated,
            insight_id,
            ts_used,
            limit,
        )
        return updated, ts_used

    async def reformat_main_insight_why_structure_with_sonnet(
        self,
        *,
        run_timestamp: Optional[datetime] = None,
        insight_id: Optional[UUID] = None,
        limit: int = 2000,
    ) -> tuple[int, Optional[datetime]]:
        """Reformat ``why`` for portal layout (``<sub>**…**<sub>``, bullets, ** emphasis) via Sonnet.

        LLM-only mode: if model wording drifts from the input, retain Sonnet output
        and only apply structural normalization.
        """
        ts_used: Optional[datetime] = None
        if insight_id is not None:
            rows = await self.store.list_main_insights_for_why_summarization(
                insight_id=insight_id,
                limit=1,
            )
            ts_used = rows[0]["run_timestamp"] if rows else None
        else:
            ts_used = run_timestamp or await self.store.get_latest_main_insight_run_timestamp()
            if not ts_used:
                logger.info("No main_insights rows; skip why structure reformat.")
                return 0, None
            rows = await self.store.list_main_insights_for_why_summarization(
                run_timestamp=ts_used,
                limit=limit,
            )
        if not rows:
            return 0, ts_used

        updated = 0
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        for row in rows:
            why_raw = self._clip_text(row.get("why"), max_chars=28000)
            if not why_raw.strip():
                continue
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))
            dn = row.get("dimension_name")
            dv = row.get("dimension_value")
            dname = (dn or "").strip() or "n/a"
            dval = (dv or "").strip() or "n/a"
            user_prompt = _WHY_REFORMAT_STRUCTURE_SONNET_USER_PROMPT_TEMPLATE.format(
                insight_title=(row.get("title") or "").strip(),
                kpi=(row.get("kpi") or "").strip(),
                dimension_name=dname,
                dimension_value=dval,
                why=why_raw,
            )
            try:
                async with sem:
                    out_raw = await self._call_gpt4o(
                        _WHY_REFORMAT_STRUCTURE_SONNET_SYSTEM_PROMPT,
                        user_prompt,
                        context=f"why_reformat_structure_{row.get('kpi') or 'row'}",
                        max_tokens=6000,
                        temperature=0.0,
                        prompt_log_category="main_insight",
                        prompt_log_subfolder="why_reformat_structure_sonnet",
                    )
                cleaned = repair_broken_bold_fragments(
                    InsightEngine._strip_code_fence(out_raw)
                ).strip()
                cleaned = _why_unescape_literal_newlines(cleaned).strip()
                if not cleaned:
                    continue
                base_norm = _normalize_why_for_drift_check(why_raw)
                out_norm = _normalize_why_for_drift_check(cleaned)
                if base_norm != out_norm:
                    logger.info(
                        "reformat_main_insight_why_structure_with_sonnet prose drift insight_id=%s; accepting Sonnet output (llm_only)",
                        uid,
                    )
                final_why = normalize_why_subsection_structure(cleaned)
                if not final_why.strip():
                    continue
                n = await self.store.update_main_insight_why(uid, final_why)
                updated += n
            except Exception as exc:
                logger.error(
                    "WHY structure reformat failed for insight_id=%s: %s", uid, exc
                )
        logger.info(
            "reformat_main_insight_why_structure_with_sonnet updated %s row(s) (insight_id=%s run_timestamp=%s limit=%s)",
            updated,
            insight_id,
            ts_used,
            limit,
        )
        return updated, ts_used

    # ─── REFINEMENT APIs (Opus post-generation polish) ────────────────────────────

    async def refine_insight_what_with_opus(
        self,
        *,
        run_timestamp: Optional[datetime] = None,
        insight_id: Optional[UUID] = None,
        limit: int = 2000,
    ) -> tuple[int, Optional[datetime]]:
        """Refine ``insight`` (problem_statement) and ``insight_summary`` using Opus for language quality."""
        ts_used: Optional[datetime] = None
        if insight_id is not None:
            rows = await self.store.list_main_insights_for_refinement(
                insight_id=insight_id, limit=1
            )
            ts_used = rows[0]["run_timestamp"] if rows else None
        else:
            ts_used = run_timestamp or await self.store.get_latest_main_insight_run_timestamp()
            if not ts_used:
                logger.info("No main_insights rows; skip what refinement.")
                return 0, None
            rows = await self.store.list_main_insights_for_refinement(
                run_timestamp=ts_used, limit=limit
            )
        if not rows:
            return 0, ts_used

        updated = 0
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        for row in rows:
            insight_raw = self._clip_text(row.get("insight"), max_chars=28000)
            if not insight_raw:
                continue
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))
            existing_summary = (row.get("insight_summary") or "").strip()
            user_prompt = _REFINE_WHAT_USER_PROMPT_TEMPLATE.format(
                title=(row.get("title") or "").strip(),
                kpi=(row.get("kpi") or "").strip(),
                dimension_name=(row.get("dimension_name") or "").strip() or "n/a",
                dimension_value=(row.get("dimension_value") or "").strip() or "n/a",
                severity=(row.get("severity") or "").strip() or "n/a",
                insight=insight_raw,
                insight_summary=existing_summary,
                why=self._clip_text(row.get("why"), max_chars=12000) or "",
                impact_insight=self._clip_text(row.get("impact_insight"), max_chars=6000) or "",
            )
            try:
                async with sem:
                    out_raw = await self._call_gpt4o(
                        _REFINE_WHAT_SYSTEM_PROMPT,
                        user_prompt,
                        context=f"refine_what_{row.get('kpi') or 'row'}",
                        max_tokens=3000,
                        temperature=0.0,
                        prompt_log_category="main_insight",
                        prompt_log_subfolder="refine_what_opus",
                    )
                cleaned = InsightEngine._strip_code_fence(out_raw).strip()
                parsed = json.loads(cleaned)
                exec_paragraph = (parsed.get("executive_paragraph") or "").strip()
                reformatted_bullets = (parsed.get("reformatted_bullets") or "").strip()
                if not exec_paragraph and not reformatted_bullets:
                    continue
                exec_paragraph = repair_broken_bold_fragments(exec_paragraph) if exec_paragraph else ""
                reformatted_bullets = repair_broken_bold_fragments(reformatted_bullets) if reformatted_bullets else ""
                # Replace insight column with exec paragraph; replace insight_summary with concise bullets
                new_insight = exec_paragraph if exec_paragraph else insight_raw
                new_summary = reformatted_bullets if reformatted_bullets else existing_summary
                n = await self.store.update_main_insight_what(uid, new_insight, new_summary)
                updated += n
            except Exception as exc:
                logger.error("refine_insight_what failed for insight_id=%s: %s", uid, exc)
        logger.info(
            "refine_insight_what_with_opus updated %s row(s) (insight_id=%s run_timestamp=%s limit=%s)",
            updated, insight_id, ts_used, limit,
        )
        return updated, ts_used

    async def refine_insight_why_with_opus(
        self,
        *,
        run_timestamp: Optional[datetime] = None,
        insight_id: Optional[UUID] = None,
        limit: int = 2000,
    ) -> tuple[int, Optional[datetime]]:
        """Refine ``why`` and ``why_insight_summary`` using Opus for language quality and formatting."""
        ts_used: Optional[datetime] = None
        if insight_id is not None:
            rows = await self.store.list_main_insights_for_refinement(
                insight_id=insight_id, limit=1
            )
            ts_used = rows[0]["run_timestamp"] if rows else None
        else:
            ts_used = run_timestamp or await self.store.get_latest_main_insight_run_timestamp()
            if not ts_used:
                logger.info("No main_insights rows; skip why refinement.")
                return 0, None
            rows = await self.store.list_main_insights_for_refinement(
                run_timestamp=ts_used, limit=limit
            )
        if not rows:
            return 0, ts_used

        updated = 0
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        for row in rows:
            why_raw = self._clip_text(row.get("why"), max_chars=28000)
            if not why_raw:
                continue
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))
            user_prompt = _REFINE_WHY_USER_PROMPT_TEMPLATE.format(
                title=(row.get("title") or "").strip(),
                kpi=(row.get("kpi") or "").strip(),
                dimension_name=(row.get("dimension_name") or "").strip() or "n/a",
                dimension_value=(row.get("dimension_value") or "").strip() or "n/a",
                severity=(row.get("severity") or "").strip() or "n/a",
                insight=self._clip_text(row.get("insight"), max_chars=12000) or "",
                insight_summary=self._clip_text(row.get("insight_summary"), max_chars=8000) or "",
                why=why_raw,
                why_insight_summary=self._clip_text(row.get("why_insight_summary"), max_chars=8000) or "",
                impact_insight=self._clip_text(row.get("impact_insight"), max_chars=6000) or "",
            )
            try:
                async with sem:
                    out_raw = await self._call_gpt4o(
                        _REFINE_WHY_SYSTEM_PROMPT,
                        user_prompt,
                        context=f"refine_why_{row.get('kpi') or 'row'}",
                        max_tokens=8000,
                        temperature=0.0,
                        prompt_log_category="main_insight",
                        prompt_log_subfolder="refine_why_opus",
                    )
                cleaned = InsightEngine._strip_code_fence(out_raw).strip()
                parsed = json.loads(cleaned)
                new_why = parsed.get("why", "").strip()
                new_why_summary = parsed.get("why_insight_summary", "").strip()
                if not new_why:
                    continue
                new_why = normalize_why_subsection_structure(
                    repair_broken_bold_fragments(new_why)
                )
                new_why_summary = repair_broken_bold_fragments(new_why_summary) if new_why_summary else ""
                n = await self.store.update_main_insight_why_and_summary(uid, new_why, new_why_summary)
                updated += n
            except Exception as exc:
                logger.error("refine_insight_why failed for insight_id=%s: %s", uid, exc)
        logger.info(
            "refine_insight_why_with_opus updated %s row(s) (insight_id=%s run_timestamp=%s limit=%s)",
            updated, insight_id, ts_used, limit,
        )
        return updated, ts_used

    async def refine_insight_summary_with_opus(
        self,
        *,
        run_timestamp: Optional[datetime] = None,
        insight_id: Optional[UUID] = None,
        limit: int = 2000,
    ) -> tuple[int, Optional[datetime]]:
        """Rewrite ``insight_summary`` into concise CFO-style bullets using Opus."""
        ts_used: Optional[datetime] = None
        if insight_id is not None:
            rows = await self.store.list_main_insights_for_refinement(
                insight_id=insight_id, limit=1
            )
            ts_used = rows[0]["run_timestamp"] if rows else None
        else:
            ts_used = run_timestamp or await self.store.get_latest_main_insight_run_timestamp()
            if not ts_used:
                logger.info("No main_insights rows; skip summary refinement.")
                return 0, None
            rows = await self.store.list_main_insights_for_refinement(
                run_timestamp=ts_used, limit=limit
            )
        if not rows:
            return 0, ts_used

        updated = 0
        sem = asyncio.Semaphore(self._main_insights_llm_concurrency)
        for row in rows:
            existing_summary = (row.get("insight_summary") or "").strip()
            if not existing_summary:
                continue
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))
            user_prompt = _REFINE_SUMMARY_USER_PROMPT_TEMPLATE.format(
                title=(row.get("title") or "").strip(),
                kpi=(row.get("kpi") or "").strip(),
                dimension_name=(row.get("dimension_name") or "").strip() or "n/a",
                dimension_value=(row.get("dimension_value") or "").strip() or "n/a",
                severity=(row.get("severity") or "").strip() or "n/a",
                insight=self._clip_text(row.get("insight"), max_chars=12000) or "",
                insight_summary=existing_summary,
                why=self._clip_text(row.get("why"), max_chars=12000) or "",
                impact_insight=self._clip_text(row.get("impact_insight"), max_chars=6000) or "",
            )
            try:
                async with sem:
                    out_raw = await self._call_gpt4o(
                        _REFINE_SUMMARY_SYSTEM_PROMPT,
                        user_prompt,
                        context=f"refine_summary_{row.get('kpi') or 'row'}",
                        max_tokens=2000,
                        temperature=0.0,
                        prompt_log_category="main_insight",
                        prompt_log_subfolder="refine_summary_opus",
                    )
                cleaned = InsightEngine._strip_code_fence(out_raw).strip()
                # Output is plain bullets, not JSON
                if not cleaned:
                    continue
                cleaned = repair_broken_bold_fragments(cleaned)
                n = await self.store.update_main_insight_summary_only(uid, cleaned)
                updated += n
            except Exception as exc:
                logger.error("refine_insight_summary failed for insight_id=%s: %s", uid, exc)
        logger.info(
            "refine_insight_summary_with_opus updated %s row(s) (insight_id=%s run_timestamp=%s limit=%s)",
            updated, insight_id, ts_used, limit,
        )
        return updated, ts_used

    async def generate_recommended_actions(
        self,
        run_timestamp: Optional[datetime] = None,
        insight_id: Optional[UUID] = None,
        *,
        skip_existing: bool = False,
    ) -> tuple[int, Optional[datetime]]:
        """LLM: fill ``recommended_actions`` (exactly three ``• `` lines) per main insight row.

        When ``insight_id`` is set, only that row is processed. Otherwise all rows for
        ``run_timestamp`` are processed; if ``run_timestamp`` is omitted, the latest
        ``run_timestamp`` among ``main_insights`` is used.
        """
        ts_used: Optional[datetime] = None
        if insight_id is not None:
            rows = await self.store.list_main_insights_for_recommended_actions(
                insight_id=insight_id,
                skip_existing=skip_existing,
            )
            ts_used = rows[0]["run_timestamp"] if rows else None
        else:
            ts_used = run_timestamp or await self.store.get_latest_main_insight_run_timestamp()
            if not ts_used:
                logger.info("No main_insights rows; skip recommended actions.")
                return 0, None
            rows = await self.store.list_main_insights_for_recommended_actions(
                run_timestamp=ts_used,
                skip_existing=skip_existing,
            )

        if not rows:
            return 0, ts_used

        updated = 0
        for row in rows:
            iid = row["insight_id"]
            uid = iid if isinstance(iid, UUID) else UUID(str(iid))

            insight = self._clip_text(row.get("insight"))
            why = self._clip_text(row.get("why"))
            ins_sum = self._clip_text(row.get("insight_summary"))
            why_sum = self._clip_text(row.get("why_insight_summary"))
            impact = self._clip_text(row.get("impact_insight"))
            if not any([insight, why, ins_sum, why_sum, impact]):
                logger.info("Skipping recommended actions for insight_id=%s (no text fields)", uid)
                continue

            user_prompt = (
                f"Title: {row.get('title') or ''}\n"
                f"Primary metric: {row.get('kpi') or ''}\n"
                f"Dimension: {(row.get('dimension_name') or '').strip()} — {(row.get('dimension_value') or '').strip()}\n\n"
                f"=== INSIGHT (what) ===\n{insight}\n\n"
                f"=== WHY ===\n{why}\n\n"
                f"=== INSIGHT SUMMARY ===\n{ins_sum}\n\n"
                f"=== WHY INSIGHT SUMMARY ===\n{why_sum}\n\n"
                f"=== IMPACT INSIGHT ===\n{impact}\n"
            )
            try:
                raw = await self._call_gpt4o(
                    _RECOMMENDED_ACTIONS_SYSTEM_PROMPT,
                    user_prompt,
                    context=f"rec_actions_{row.get('kpi') or 'row'}",
                    prompt_log_category="main_insight",
                    prompt_log_subfolder="recommended_actions",
                )
                base_actions = self._format_recommended_actions_for_db(raw)
                # Strip any ** formatting the model might have included
                normalized = re.sub(r"\*\*(.+?)\*\*", r"\1", base_actions) if base_actions else ""
                if not normalized.strip():
                    continue
                n = await self.store.update_main_insight_recommended_actions(uid, normalized)
                updated += n
            except Exception as exc:
                logger.error("Recommended actions failed for insight_id=%s: %s", uid, exc)

        logger.info("Updated recommended_actions on %d main insight row(s).", updated)
        return updated, ts_used

    def _build_executive_summary_blocks(self, rows: list[dict]) -> list[str]:
        blocks: list[str] = []
        for i, row in enumerate(rows, start=1):
            title = (row.get("title") or "").strip()
            kpi = (row.get("kpi") or row.get("kpi_family") or "").strip()
            dim = (row.get("dimension_name") or "").strip()
            dval = (row.get("dimension_value") or "").strip()
            period = (row.get("period") or "").strip()
            why = self._clip_text(row.get("why"), max_chars=1200) or ""
            insight = self._clip_text(row.get("insight"), max_chars=600) or ""
            summary = self._clip_text(row.get("insight_summary"), max_chars=400) or ""
            if not any([title, kpi, why, insight, summary]):
                continue
            blocks.append(
                f"--- Insight {i} ---\n"
                f"Period: {period}\n"
                f"Title: {title}\n"
                f"KPI: {kpi}\n"
                f"Slice: {dim} — {dval}\n"
                f"Why: {why}\n"
                f"What: {insight}\n"
                f"Summary: {summary}\n"
            )
        return blocks

    async def _group_highlight_for_rows(
        self, rows: list[dict], *, bucket: str
    ) -> dict[str, Any] | None:
        """Synthesize ONE combined insight from all a group's insights + an action."""
        blocks = self._build_executive_summary_blocks(rows)
        if not blocks:
            return None
        user_prompt = (
            f"KPI group insights ({bucket.upper()}). Synthesize ALL of them into one.\n\n"
            + "\n".join(blocks)
        )
        raw = await self._call_gpt4o(
            _GROUP_HIGHLIGHT_SYSTEM_PROMPT,
            user_prompt,
            context=f"group_highlight_{bucket}",
            max_tokens=600,
            temperature=0.2,
            prompt_log_category="main_insight",
            prompt_log_subfolder=f"group_highlight_{bucket}",
        )
        try:
            parsed = _parse_narrative_json(raw)
        except Exception:
            logger.exception("Group highlight JSON parse failed (%s).", bucket)
            return None
        return _normalize_group_highlight(parsed)

    def _build_sales_rep_signal_blocks(self, rows: list[dict]) -> list[str]:
        """One block per rep signal. Rep-level data lives in signal_log, not main_insights."""
        blocks: list[str] = []
        for i, row in enumerate(rows, start=1):
            kpi = (row.get("kpi_name") or "").strip()
            if not kpi:
                continue
            cur = row.get("current_kpi_value")
            prev = row.get("prev_kpi_value")
            change = ""
            try:
                if prev not in (None, "") and float(prev) != 0 and cur not in (None, ""):
                    pct = (float(cur) - float(prev)) / abs(float(prev)) * 100.0
                    change = f"{pct:+.1f}%"
            except (TypeError, ValueError):
                change = ""
            blocks.append(
                f"--- Signal {i} ---\n"
                f"KPI: {kpi}\n"
                f"Signal: {(row.get('signal_name') or '').strip()}\n"
                f"Current: {cur}\n"
                f"Previous: {prev}\n"
                f"Change: {change or 'n/a'}\n"
                f"Severity: {(row.get('severity') or '').strip()}\n"
            )
        return blocks

    async def summarize_sales_rep_executive(
        self,
        *,
        sales_rep: str,
        group: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Executive summary for one sales rep, built from their signal_log rows.

        ``maininsightsportal`` holds no dimension_name='sales_rep' rows, so the rep
        narrative cannot come from the main-insights path; signals are the source.
        When ``group`` is given, only that card's KPIs feed the summary.
        """
        from .portal_kpi_groups import resolve_group_kpis

        rep = (sales_rep or "").strip()
        if not rep:
            raise ValueError("sales_rep is required")
        try:
            kpi_set = resolve_group_kpis(group)
        except KeyError as e:
            raise ValueError(f"unknown group: {group}") from e

        rows = await self.store.list_sales_rep_signals(rep, kpi_names=kpi_set, limit=limit)
        base: dict[str, Any] = {
            "sales_rep": rep,
            "group": group,
            "signal_count": len(rows),
            "pointers": [],
        }
        blocks = self._build_sales_rep_signal_blocks(rows)
        if not blocks:
            return base

        scope = f"KPI group {group}" if group else "all KPIs"
        user_prompt = (
            f"Sales rep: {rep}\n"
            f"Scope: {scope}\n"
            f"Signals: {len(rows)}\n\n" + "\n".join(blocks)
        )
        raw = await self._call_gpt4o(
            _EXECUTIVE_SUMMARY_COLORED_SYSTEM_PROMPT,
            user_prompt,
            context=f"sales_rep_summary_{rep}",
            max_tokens=1000,
            temperature=0.2,
            prompt_log_category="main_insight",
            prompt_log_subfolder="sales_rep_summary",
        )
        parsed = _parse_narrative_json(raw)
        base["pointers"] = _normalize_executive_pointers_colored(parsed.get("pointers"))

        if group:
            highlight_prompt = (
                f"Sales rep {rep}, KPI group {group}. Synthesize ALL of these signals into one.\n\n"
                + "\n".join(blocks)
            )
            raw2 = await self._call_gpt4o(
                _GROUP_HIGHLIGHT_SYSTEM_PROMPT,
                highlight_prompt,
                context=f"sales_rep_highlight_{rep}",
                max_tokens=600,
                temperature=0.2,
                prompt_log_category="main_insight",
                prompt_log_subfolder="sales_rep_highlight",
            )
            try:
                highlight = _normalize_group_highlight(_parse_narrative_json(raw2))
            except Exception:
                logger.exception("Sales-rep highlight JSON parse failed (%s / %s).", rep, group)
                highlight = None
            if highlight:
                base["best_insight"] = highlight["best_insight"]
                base["recommended_action"] = highlight["recommended_action"]
        return base

    async def _executive_summary_for_rows(
        self,
        rows: list[dict],
        *,
        bucket: str,
        run_timestamp: datetime,
        period_label: str | None = None,
        include_group_highlight: bool = False,
    ) -> dict[str, Any]:
        from .portal_period import filter_rows_by_period_label, period_window_from_rows

        scoped = filter_rows_by_period_label(rows, period_label) if period_label else rows
        if period_label and not scoped and rows:
            scoped = rows
        window = period_window_from_rows(scoped, period_label=period_label)
        blocks = self._build_executive_summary_blocks(scoped)
        base: dict[str, Any] = {
            "period": (window or {}).get("period") or period_label,
            "period_start": (window or {}).get("period_start"),
            "period_end": (window or {}).get("period_end"),
            "insight_count": len(scoped),
            "pointers": [],
        }
        if not blocks:
            return base

        user_prompt = (
            f"Reporting window: {bucket.upper()}\n"
            f"Period label: {base.get('period') or 'n/a'}\n"
            f"Run timestamp: {run_timestamp.isoformat()}\n"
            f"Insights in this window: {len(scoped)}\n\n"
            + "\n".join(blocks)
        )
        raw = await self._call_gpt4o(
            _EXECUTIVE_SUMMARY_COLORED_SYSTEM_PROMPT,
            user_prompt,
            context=f"executive_summary_{bucket}",
            max_tokens=1000,
            temperature=0.2,
            prompt_log_category="main_insight",
            prompt_log_subfolder=f"executive_summary_{bucket}",
        )
        parsed = _parse_narrative_json(raw)
        base["pointers"] = _normalize_executive_pointers_colored(parsed.get("pointers"))

        # When scoped to a KPI-card group, also append one synthesized insight
        # (heading + description) combining all the group's insights, plus an action.
        if include_group_highlight:
            highlight = await self._group_highlight_for_rows(scoped, bucket=bucket)
            if highlight:
                base["best_insight"] = highlight["best_insight"]
                base["recommended_action"] = highlight["recommended_action"]
        return base

    async def summarize_main_insights_executive_split(
        self,
        *,
        run_timestamp: Optional[datetime] = None,
        limit: int = 500,
        period_type: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        """Five GPT-4o pointers for portal monthly / weekly windows.

        When ``group`` is given, only insights whose manual ``group_name`` column
        equals that group (a clicked KPI-card group) feed the summary. Otherwise
        all insights are summarized (overall).
        """
        from .portal_kpi_groups import row_in_group
        from .portal_period import (
            PORTAL_MONTHLY_PERIOD_LABEL,
            PORTAL_WEEKLY_PERIOD_LABEL,
            InsightPeriodBucket,
            split_portal_insight_rows,
        )

        if run_timestamp is not None:
            ts_used = run_timestamp
        else:
            ts_used = await self.store.get_latest_main_insight_run_timestamp()
        if not ts_used:
            logger.info("Executive summary: no main_insights run_timestamp found.")
            return {"run_timestamp": None, "monthly": None, "weekly": None}

        rows = await self.store.list_main_insight_rows(
            run_timestamp=ts_used,
            limit=max(1, min(limit, 5000)),
            pascal_case=False,
        )
        if group:
            rows = [r for r in rows if row_in_group(r, group)]
        monthly_rows, weekly_rows = split_portal_insight_rows(rows)
        if not weekly_rows and rows:
            monthly_ids = {id(r) for r in monthly_rows}
            weekly_rows = [r for r in rows if id(r) not in monthly_ids] or list(rows)

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

        include_highlight = bool(group)
        monthly = None
        weekly = None
        if want_monthly:
            monthly = await self._executive_summary_for_rows(
                monthly_rows,
                bucket="monthly",
                run_timestamp=ts_used,
                period_label=PORTAL_MONTHLY_PERIOD_LABEL,
                include_group_highlight=include_highlight,
            )
        if want_weekly:
            weekly = await self._executive_summary_for_rows(
                weekly_rows,
                bucket="weekly",
                run_timestamp=ts_used,
                period_label=PORTAL_WEEKLY_PERIOD_LABEL,
                include_group_highlight=include_highlight,
            )
        return {
            "run_timestamp": ts_used,
            "monthly": monthly,
            "weekly": weekly,
        }
