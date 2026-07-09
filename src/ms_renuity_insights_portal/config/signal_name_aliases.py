"""Legacy signal name aliases — map retired names to current ``config_signalsrenuitycrm`` rows."""

from __future__ import annotations

SIGNAL_NAME_ALIASES: dict[str, str] = {
    "week_spike": "weekly_growth",
    "week_drop": "weekly_degrowth",
}


def normalize_signal_name(signal_name: str | None) -> str:
    """Return the canonical signal name (unchanged when already current)."""
    raw = (signal_name or "").strip()
    if not raw:
        return ""
    return SIGNAL_NAME_ALIASES.get(raw, raw)


def normalize_signal_names(signal_names: list[str] | None) -> list[str]:
    """Map legacy names and preserve order (first occurrence only)."""
    if not signal_names:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for name in signal_names:
        canon = normalize_signal_name(str(name))
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
    return out
