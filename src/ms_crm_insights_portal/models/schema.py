"""Resolve the PostgreSQL schema name for Insights tables."""
import os

# K8s/Helm often sets PLATFORM_DATABASE__SCHEMA="" (empty). getenv still returns ""
# and would break SQL like "CREATE VIEW .vw_..." — treat blank as unset.
_raw = (os.getenv("PLATFORM_DATABASE__SCHEMA") or "").strip()
SCHEMA = _raw if _raw else "insights"
