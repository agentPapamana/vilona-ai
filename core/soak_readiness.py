"""Soak readiness gate.

Decides whether PAPER-mode evidence is strong enough to consider a canary stage.
Every blocker is named explicitly so the answer is actionable rather than a bare
boolean. The gate never authorises live trading on its own: it only reports.
"""
from __future__ import annotations

from typing import Any, Dict, List

DEFAULT_MIN_JUDGMENTS = 50
DEFAULT_MIN_PAPER_TRADES = 50
DEFAULT_MAX_ERROR_RATE = 0.10
DEFAULT_MIN_APPROVAL_SHARE = 0.02
DEFAULT_MAX_DUPLICATE_SHARE = 0.05


def evaluate_soak_readiness(
    stats: Dict[str, int],
    paper_trades: int,
    min_judgments: int = DEFAULT_MIN_JUDGMENTS,
    min_paper_trades: int = DEFAULT_MIN_PAPER_TRADES,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
    min_approval_share: float = DEFAULT_MIN_APPROVAL_SHARE,
    paper_rows: int | None = None,
    max_duplicate_share: float = DEFAULT_MAX_DUPLICATE_SHARE,
) -> Dict[str, Any]:
    """Return readiness with an explicit list of blockers.

    A high veto share is treated as a blocker because a strategy that is vetoed
    almost every time has produced no execution evidence: the soak would look
    complete while never having exercised order handling.

    ``paper_trades`` must be the count of DISTINCT setups. ``paper_rows`` is the
    raw row count; the gap between them is duplicate evidence, which inflates the
    soak and is reported as its own blocker instead of silently counting.
    """
    total = int(stats.get("total", 0) or 0)
    approved = int(stats.get("approved", 0) or 0)
    vetoed = int(stats.get("vetoed", 0) or 0)
    errors = int(stats.get("errors", 0) or 0)
    no_verdict = int(stats.get("no_verdict", 0) or 0)

    blockers: List[str] = []
    error_rate = (errors / total) if total else 0.0
    approval_share = (approved / total) if total else 0.0

    if total < min_judgments:
        blockers.append(
            f"only {total} judge judgment(s) recorded; {min_judgments} required"
        )

    if int(paper_trades) < min_paper_trades:
        blockers.append(
            f"only {int(paper_trades)} distinct paper trade setup(s) recorded; {min_paper_trades} required"
        )

    duplicate_rows = 0
    duplicate_share = 0.0
    if paper_rows is not None:
        duplicate_rows = max(0, int(paper_rows) - int(paper_trades))
        duplicate_share = (duplicate_rows / int(paper_rows)) if int(paper_rows) else 0.0
        if duplicate_share > max_duplicate_share:
            blockers.append(
                f"duplicate paper evidence {duplicate_share:.1%} exceeds "
                f"{max_duplicate_share:.0%} ({duplicate_rows}/{int(paper_rows)} duplicate rows)"
            )

    if no_verdict > 0:
        blockers.append(
            f"{no_verdict} judgment(s) recorded with no verdict; "
            "those outcomes are undetermined and must be explained"
        )

    if error_rate > max_error_rate:
        blockers.append(
            f"judge error rate {error_rate:.1%} exceeds {max_error_rate:.0%} "
            f"({errors}/{total} judgments failed)"
        )

    if total >= min_judgments and approval_share < min_approval_share:
        blockers.append(
            f"approval share {approval_share:.2%} is below the {min_approval_share:.0%} "
            f"floor ({approved} approved, {vetoed} vetoed): the strategy is not "
            "producing executable setups, so order handling is untested"
        )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "total": total,
        "approved": approved,
        "vetoed": vetoed,
        "errors": errors,
        "no_verdict": no_verdict,
        "error_rate": error_rate,
        "approval_share": approval_share,
        "paper_trades": int(paper_trades),
        "paper_rows": int(paper_rows) if paper_rows is not None else int(paper_trades),
        "duplicate_rows": duplicate_rows,
        "duplicate_share": duplicate_share,
    }
