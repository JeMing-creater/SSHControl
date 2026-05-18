from __future__ import annotations


def parse_percent(value: str) -> float:
    value = (value or "").replace("%", "").strip()
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_memory_usage(value: str) -> float:
    chunk = (value or "").split("/")[0].strip().upper()
    if not chunk:
        return 0.0
    units = {"GIB": 1.0, "MIB": 1 / 1024, "KIB": 1 / (1024**2), "B": 1 / (1024**3)}
    for suffix, factor in units.items():
        if chunk.endswith(suffix):
            try:
                return float(chunk[: -len(suffix)].strip()) * factor
            except ValueError:
                return 0.0
    try:
        return float(chunk)
    except ValueError:
        return 0.0
