"""Project-root entry point for uvicorn.

Usage:
    python main.py                       # starts on 0.0.0.0:8010 (override with PLATFORM_SERVICE__PORT)
    uvicorn insights_engine.app:app --reload --port 8010

On Windows, ``reload=True`` can spawn a child whose imports differ from the
parent, so Swagger may not match the app you think is running. We default to
``reload=False``. For auto-reload during development set env
``UVICORN_RELOAD=1`` (or ``true``).
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# Reload subprocess inherits env; without this, ``insights_engine`` may not resolve.
_sep = os.pathsep
_existing = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = _SRC if not _existing else _SRC + _sep + _existing

from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))

from insights_engine.app import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    _reload = os.getenv("UVICORN_RELOAD", "").strip().lower() in ("1", "true", "yes", "on")
    _kw = dict(
        host="0.0.0.0",
        port=int(os.getenv("PLATFORM_SERVICE__PORT", "8010")),
        reload=_reload,
    )
    if _reload:
        _kw["reload_dirs"] = [_ROOT]
    uvicorn.run("insights_engine.app:app", **_kw)
