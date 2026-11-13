"""Pure report window helpers shared by the CLI and fixture tests."""
from __future__ import annotations

from typing import Any, Iterable


def filter_judge_rows(rows: Iterable[dict[str, Any]], since: float | None) -> list[dict[str, Any]]:
    """Filter judge decisions by epoch timestamp; None preserves cumulative rows."""
    result = []
    for row in rows:
        if since is None or float(row.get("created_at", 0) or 0) >= since:
            result.append(row)
    return result


def summarize_judge_rows(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Return aggregate counts and per-symbol metrics for selected decisions."""
    stats = {"total": 0, "approved": 0, "vetoed": 0, "errors": 0, "no_verdict": 0}
    grouped: dict[str, list[dict[str, Any]]] = {}
    verdict_map = {"APPROVE": "approved", "VETO": "vetoed", "ERROR": "errors"}
    for row in rows:
        stats["total"] += 1
        verdict = row.get("verdict")
        bucket = verdict_map.get(verdict, "no_verdict") if isinstance(verdict, str) else "no_verdict"
        stats[bucket] += 1
        symbol = row.get("symbol") or ""
        grouped.setdefault(str(symbol), []).append(row)
    metrics = {}
    for symbol, group in sorted(grouped.items()):
        n = len(group)
        errors = sum(row.get("verdict") == "ERROR" for row in group)
        latencies = [float(row["latency_ms"]) for row in group if row.get("latency_ms") is not None]
        metrics[symbol] = {
            "total": n,
            "approved": sum(row.get("verdict") == "APPROVE" for row in group),
            "vetoed": sum(row.get("verdict") == "VETO" for row in group),
            "errors": errors,
            "error_rate": errors / n if n else 0.0,
            "avg_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
        }
    return stats, metrics
