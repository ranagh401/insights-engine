"""APScheduler-based scheduler for periodic KPI signal-detection jobs.

Reads active ``SignalJobConfig`` rows from the config loader, registers
one ``IntervalTrigger`` per job, and runs a separate sweep for WHY
analysis on unprocessed signals.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config.config_loader import ConfigLoader
from ..config.models import SignalJobConfig
from ..engine.kpi_engine import KPIEngine
from ..engine.signal_detector import SignalDetector
from ..engine.why_analyzer import WhyAnalyzer
from ..settings import Settings

logger = logging.getLogger(__name__)


class KPIScheduler:
    """Registers and runs periodic KPI fetch / signal-detection jobs."""

    def __init__(
        self,
        config_loader: ConfigLoader,
        kpi_engine: KPIEngine,
        signal_detector: SignalDetector,
        why_analyzer: WhyAnalyzer,
        settings: Settings,
    ) -> None:
        self._loader = config_loader
        self._engine = kpi_engine
        self._detector = signal_detector
        self._why = why_analyzer
        self._settings = settings
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        """Load active jobs and start the APScheduler event loop."""
        jobs = await self._loader.get_signal_jobs(active_only=True)
        logger.info("Registering %d signal job(s).", len(jobs))

        for job in jobs:
            self._scheduler.add_job(
                self._run_job,
                trigger=IntervalTrigger(minutes=job.frequency_minutes),
                args=[job],
                id=f"kpi_job_{job.job_id}_{job.dimension_name}",
                replace_existing=True,
            )

        self._scheduler.add_job(
            self._sweep_why,
            trigger=IntervalTrigger(
                minutes=self._settings.WHY_ANALYSIS_SWEEP_MINUTES
            ),
            id="why_sweep",
            replace_existing=True,
        )

        self._scheduler.start()
        logger.info("KPI scheduler started.")

    async def _run_job(self, job: SignalJobConfig) -> None:
        """Execute a single KPI fetch + signal-detection cycle."""
        import time

        t0 = time.monotonic()
        today = date.today()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        phase1 = await self._engine.run_phase1(
            job, start_of_week, end_of_week
        )

        if not phase1:
            logger.info(
                "Job %s skipped — no pbi_measure_name configured. "
                "SQL engine will handle this KPI.",
                job.job_id,
            )
            return

        total_rows = 0
        total_signals = 0

        for dim_name, kpi_rows in phase1.items():
            total_rows += len(kpi_rows)
            signals = await self._detector.detect_and_persist(
                kpi_rows, job, grain=dim_name
            )
            total_signals += len(signals)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "Job %s complete — kpi=%s dims=%d rows=%d signals=%d elapsed=%dms",
            job.job_id,
            job.kpi_name,
            len(phase1),
            total_rows,
            total_signals,
            elapsed_ms,
        )

    async def _sweep_why(self) -> None:
        """Periodic sweep: run WHY analysis on unprocessed signals."""
        n_done = await self._why.analyze_unprocessed()
        if n_done:
            logger.info("WHY sweep processed %d signal(s).", n_done)

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
