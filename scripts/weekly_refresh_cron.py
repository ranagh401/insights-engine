"""Weekly refresh orchestrator — end-to-end signal-to-summary pipeline.

Runs unattended (system cron). Steps:

  1. Resolve the latest reporting period from Power BI, write it to
     ``insights.portal_current_period`` (the frontend reads it via
     GET /pbi/portal/current-period) and stamp it onto every
     ``config_signaljobsrenuitycrm.filters.period``.
  2. POST /pbi/trigger/run-all-signals, then poll signal_log + why_results until
     they stop growing (the trigger is fire-and-forget).
  3. POST /pbi/insights/main-insights/generate-standard — builds maininsightscrm
     from the fresh signals + whys. (Hidden dependency: finalcrm reads this table.)
  4. POST /pbi/portal/main-insights/backfill-group-names — tag the new rows so the
     group-scoped summaries resolve.
  5. POST /pbi/portal/main-insights/executive-summary/refresh — finalcrm (LLM).
  6. POST /pbi/portal/main-insights/executive-summary/sales-rep/refresh —
     finalcrmsalesrep (LLM).
  7. Email stakeholders a success/failure report.

Nothing here is destructive on failure: a step that raises aborts the run and the
email reports which step failed. Use ``--dry-run`` to walk the steps without POSTing
the LLM/trigger calls or sending mail.

Config (env / .env):
  WEEKLY_REFRESH__API_BASE          default http://localhost:8014
  WEEKLY_REFRESH__PERIOD_DAX        DAX evaluated for the period (see _default_period_dax)
  WEEKLY_REFRESH__PERIOD_DAYS       window length when only an end date is returned (default 7)
  WEEKLY_REFRESH__SIGNAL_POLL_SECONDS   poll interval while waiting on signals (default 30)
  WEEKLY_REFRESH__SIGNAL_TIMEOUT_SECONDS  give up waiting after this (default 3600)
  WEEKLY_REFRESH__HTTP_TIMEOUT_SECONDS    per-request timeout for LLM steps (default 1800)
  WEEKLY_REFRESH__SALES_REP_MIN_SIGNALS   min signals per rep to refresh (default 1)
  SMTP creds — see _send_email (all optional; email is skipped + logged if unset)

Usage:
  python scripts/weekly_refresh_cron.py --dry-run
  python scripts/weekly_refresh_cron.py --run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

import httpx  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s weekly-refresh %(message)s",
)
log = logging.getLogger("weekly_refresh")


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


API_BASE = _env("WEEKLY_REFRESH__API_BASE", "http://localhost:8014").rstrip("/")
PERIOD_DAYS = int(_env("WEEKLY_REFRESH__PERIOD_DAYS", "7"))
POLL_SECONDS = int(_env("WEEKLY_REFRESH__SIGNAL_POLL_SECONDS", "30"))
SIGNAL_TIMEOUT = int(_env("WEEKLY_REFRESH__SIGNAL_TIMEOUT_SECONDS", "3600"))
HTTP_TIMEOUT = float(_env("WEEKLY_REFRESH__HTTP_TIMEOUT_SECONDS", "1800"))
REP_MIN_SIGNALS = int(_env("WEEKLY_REFRESH__SALES_REP_MIN_SIGNALS", "1"))


def _default_period_dax() -> str:
    """DAX whose single row yields the current window.

    Verified against the CRM model: the ``[Period]`` measure returns a string like
    ``"14-07-2026 to 21-07-2026"`` (``DD-MM-YYYY to DD-MM-YYYY``). Override with
    WEEKLY_REFRESH__PERIOD_DAX if the measure name/shape changes.
    """
    return 'EVALUATE ROW("Period", [Period])'


# ── period parsing ───────────────────────────────────────────────────────────
# The [Period] measure's own format: "DD-MM-YYYY to DD-MM-YYYY".
_RANGE_RE = re.compile(
    r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s*(?:to|[-–—])\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})",
    re.IGNORECASE,
)
# Fallback label form: "DD-DD Mon" (e.g. "14-21 Jul").
_LABEL_RE = re.compile(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3,})")


def _period_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.day}-{end.day} {end.strftime('%b')}"
    return f"{start.day} {start.strftime('%b')}-{end.day} {end.strftime('%b')}"


def _coerce_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _period_from_row(row: dict[str, Any]) -> tuple[date, date]:
    """Turn a Power BI result row into (start, end).

    Handles, in order: the ``[Period]`` "DD-MM-YYYY to DD-MM-YYYY" string; explicit
    start+end date columns; a single end/period date (→ derive a PERIOD_DAYS window);
    a "DD-DD Mon" label. Column names are matched loosely.
    """
    # 1) The [Period] range string, in any column value.
    for val in row.values():
        m = _RANGE_RE.search(str(val))
        if m:
            d1, mo1, y1, d2, mo2, y2 = (int(g) for g in m.groups())
            start, end = date(y1, mo1, d1), date(y2, mo2, d2)
            return (start, end) if start <= end else (end, start)

    # 2) Explicit start/end date columns.
    norm = {re.sub(r"[^a-z]", "", k.lower()): v for k, v in row.items()}

    def pick(*needles):
        for key, val in norm.items():
            if any(n in key for n in needles):
                d = _coerce_date(val)
                if d:
                    return d
        return None

    start = pick("periodstart", "startdate", "start", "windowstart")
    end = pick("periodend", "enddate", "end", "windowend")
    if start and end:
        return (start, end) if start <= end else (end, start)

    # 3) Single anchor date → derive a PERIOD_DAYS window.
    anchor = end or start or pick("maxdate", "date", "period")
    if anchor:
        return anchor - timedelta(days=PERIOD_DAYS - 1), anchor

    # 4) "DD-DD Mon" label.
    for val in row.values():
        m = _LABEL_RE.search(str(val))
        if m:
            d1, d2, mon = int(m.group(1)), int(m.group(2)), m.group(3)[:3]
            month = datetime.strptime(mon, "%b").month
            year = date.today().year
            return date(year, month, d1), date(year, month, d2)

    raise ValueError(f"Could not derive a period from Power BI row: {row!r}")


# ── step 1: period ───────────────────────────────────────────────────────────
async def step1_period(store, dry_run: bool) -> tuple[date, date, str]:
    from ms_renuity_insights_portal.api.dependencies import get_dax_settings

    # Escape hatch for testing / manual runs: WEEKLY_REFRESH__PERIOD_OVERRIDE=YYYY-MM-DD:YYYY-MM-DD
    override = _env("WEEKLY_REFRESH__PERIOD_OVERRIDE")
    if override and ":" in override:
        s_raw, e_raw = override.split(":", 1)
        start, end = date.fromisoformat(s_raw.strip()), date.fromisoformat(e_raw.strip())
        label = _period_label(start, end)
        log.info("Period override in effect: %s .. %s  (%s)", start, end, label)
        if not dry_run:
            await store.set_current_period(start, end, label)
            n = await store.update_all_signal_job_periods(start, end)
            log.info("Wrote current period and stamped %d signal job(s).", n)
        else:
            log.info("[dry-run] would set current period and update all signal jobs.")
        return start, end, label

    dax = _env("WEEKLY_REFRESH__PERIOD_DAX") or _default_period_dax()
    settings = get_dax_settings()
    if settings is None:
        raise RuntimeError("Power BI not configured (PBI_WORKSPACE_ID / PBI_DATASET_ID).")

    from ms_renuity_insights_portal.powerbi.api_client import PBIClient

    client = PBIClient(
        tenant_id=settings.AZURE_TENANT_ID,
        client_id=settings.AZURE_CLIENT_ID,
        client_secret=settings.AZURE_CLIENT_SECRET,
        workspace_id=settings.PBI_WORKSPACE_ID,
        dataset_id=settings.PBI_DATASET_ID,
        max_concurrent=settings.PBI_MAX_CONCURRENT_QUERIES,
        max_retries=settings.PBI_RETRY_MAX_ATTEMPTS,
        read_timeout_seconds=settings.PBI_DAX_READ_TIMEOUT_SECONDS,
    )
    rows = await client.execute_dax(dax)
    if not rows:
        raise ValueError("Period DAX returned no rows.")
    start, end = _period_from_row(rows[0])
    label = _period_label(start, end)
    log.info("Resolved period: %s .. %s  (%s)", start, end, label)

    if dry_run:
        log.info("[dry-run] would set current period and update all signal jobs.")
        return start, end, label

    await store.set_current_period(start, end, label)
    n = await store.update_all_signal_job_periods(start, end)
    log.info("Wrote current period and stamped %d signal job(s).", n)
    return start, end, label


# ── step 2: signals ──────────────────────────────────────────────────────────
async def _raw_counts(store) -> tuple[int, int]:
    from sqlalchemy import text as _text

    from ms_renuity_insights_portal.models.schema import SCHEMA

    async with store._sf() as s:  # noqa: SLF001 — internal session factory, read-only
        sig = (await s.execute(_text(f"select count(*) from {SCHEMA}.signal_log"))).scalar()
        why = (await s.execute(_text(f"select count(*) from {SCHEMA}.why_results"))).scalar()
    return int(sig or 0), int(why or 0)


async def step2_signals(client: httpx.AsyncClient, store, dry_run: bool) -> dict:
    if dry_run:
        log.info("[dry-run] would POST /pbi/trigger/run-all-signals and poll to completion.")
        return {"skipped": "dry-run"}

    before_sig, before_why = await _raw_counts(store)
    r = await client.post(f"{API_BASE}/pbi/trigger/run-all-signals")
    r.raise_for_status()
    log.info("run-all-signals accepted: %s", r.json())

    # The trigger runs in the API's background loop. Wait until both tables stop
    # growing for two consecutive polls, or until the timeout.
    deadline = time.monotonic() + SIGNAL_TIMEOUT
    last = (-1, -1)
    stable = 0
    started_growing = False
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        sig, why = await _raw_counts(store)
        if (sig, why) > (before_sig, before_why):
            started_growing = True
        if (sig, why) == last:
            stable += 1
        else:
            stable = 0
        last = (sig, why)
        log.info("polling signals: signal_log=%d why_results=%d (stable x%d)", sig, why, stable)
        if started_growing and stable >= 2:
            break
    else:
        raise TimeoutError(f"Signals did not settle within {SIGNAL_TIMEOUT}s.")

    return {"signal_log": last[0], "why_results": last[1]}


# ── steps 3-6: LLM pipeline over HTTP ────────────────────────────────────────
async def _post(client: httpx.AsyncClient, path: str, label: str, dry_run: bool) -> dict:
    if dry_run:
        log.info("[dry-run] would POST %s", path)
        return {"skipped": "dry-run"}
    log.info("POST %s ...", path)
    r = await client.post(f"{API_BASE}{path}", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    log.info("%s ok: %s", label, _short(body))
    return body


def _short(body: Any) -> str:
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if not isinstance(v, (list, dict))} or body
    return str(body)[:200]


# ── step 7: email ────────────────────────────────────────────────────────────
def _send_email(subject: str, body: str, dry_run: bool) -> str:
    host = _env("WEEKLY_REFRESH__SMTP_HOST")
    recipients = [
        a.strip() for a in (_env("WEEKLY_REFRESH__EMAIL_TO", "") or "").split(",") if a.strip()
    ]
    if not host or not recipients:
        log.warning("Email skipped — SMTP host or recipients not configured.")
        return "skipped (unconfigured)"
    if dry_run:
        log.info("[dry-run] would email %d recipient(s): %s", len(recipients), subject)
        return "skipped (dry-run)"

    port = int(_env("WEEKLY_REFRESH__SMTP_PORT", "587"))
    user = _env("WEEKLY_REFRESH__SMTP_USER")
    pwd = _env("WEEKLY_REFRESH__SMTP_PASSWORD")
    sender = _env("WEEKLY_REFRESH__EMAIL_FROM", user or "no-reply@insights")
    use_tls = (_env("WEEKLY_REFRESH__SMTP_STARTTLS", "true") or "true").lower() == "true"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=60) as smtp:
        if use_tls:
            smtp.starttls()
        if user and pwd:
            smtp.login(user, pwd)
        smtp.send_message(msg)
    log.info("Emailed %d recipient(s).", len(recipients))
    return f"sent to {len(recipients)} recipient(s)"


# ── orchestrator ─────────────────────────────────────────────────────────────
async def run(dry_run: bool) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ms_renuity_insights_portal.store.result_store import ResultStore

    dsn = _env("POSTGRES_DSN") or _env("ONEPLATFORM_DATABASE__URL")
    if not dsn:
        log.error("No database DSN (POSTGRES_DSN / ONEPLATFORM_DATABASE__URL).")
        return 2
    eng = create_async_engine(dsn)
    store = ResultStore(eng, async_sessionmaker(eng, expire_on_commit=False))

    started = datetime.now(timezone.utc)
    report: list[str] = []
    failed_step: str | None = None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            start, end, label = await step1_period(store, dry_run)
            report.append(f"1. Period set to {start}..{end} ({label}); signal jobs updated.")

            s2 = await step2_signals(client, store, dry_run)
            report.append(f"2. Signals generated: {_short(s2)}.")

            s3 = await _post(
                client,
                "/pbi/insights/main-insights/generate-standard",
                "generate-standard",
                dry_run,
            )
            report.append(f"3. Main insights generated: {_short(s3)}.")

            s4 = await _post(
                client,
                "/pbi/portal/main-insights/backfill-group-names",
                "backfill-group-names",
                dry_run,
            )
            report.append(f"4. Group names backfilled: {_short(s4)}.")

            s5 = await _post(
                client,
                "/pbi/portal/main-insights/executive-summary/refresh",
                "finalcrm refresh",
                dry_run,
            )
            report.append(f"5. finalcrm refreshed: {_short(s5)}.")

            s6 = await _post(
                client,
                f"/pbi/portal/main-insights/executive-summary/sales-rep/refresh?min_signals={REP_MIN_SIGNALS}",
                "finalcrmsalesrep refresh",
                dry_run,
            )
            report.append(f"6. finalcrmsalesrep refreshed: {_short(s6)}.")
        except Exception as e:  # noqa: BLE001 — top-level guard; we report and exit non-zero
            failed_step = str(e)
            log.exception("Pipeline failed.")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    ok = failed_step is None
    status = "SUCCESS" if ok else "FAILED"
    subject = f"[Insights CRM] Weekly refresh {status}"
    body = (
        f"Weekly refresh {status}\n"
        f"Started : {started.isoformat()}\n"
        f"Elapsed : {elapsed:.0f}s\n"
        f"Mode    : {'dry-run' if dry_run else 'live'}\n\n"
        + "\n".join(report)
        + (f"\n\nFAILED AT: {failed_step}\n" if failed_step else "\n")
    )
    try:
        email_status = _send_email(subject, body, dry_run)
        report.append(f"7. Email: {email_status}.")
    except Exception:
        log.exception("Email step failed (pipeline result unchanged).")

    await eng.dispose()
    log.info("\n%s", body)
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly insights refresh pipeline.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", action="store_true", help="execute the pipeline")
    g.add_argument("--dry-run", action="store_true", help="walk the steps without side effects")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
