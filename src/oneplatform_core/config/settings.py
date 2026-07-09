"""Configuration factory helpers."""
from pathlib import Path
from typing import Type, TypeVar

T = TypeVar("T")


def find_project_root() -> Path:
    """
    Locate the deployable project root (directory containing ``pyproject.toml``).

    Falls back to the ``power_bi_approach`` folder next to this file layout.
    """
    cwd = Path.cwd()
    for p in (cwd, *cwd.parents):
        if (p / "pyproject.toml").exists():
            return p
    # src/oneplatform_core/config/settings.py -> parents[3] == power_bi_approach
    return Path(__file__).resolve().parents[3]


def create_service_config(config_cls: Type[T]) -> T:
    """Instantiate a ``BaseSettings`` service config class (env + .env merged by pydantic)."""
    return config_cls()
