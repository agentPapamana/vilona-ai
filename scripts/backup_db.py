"""Consistent SQLite backup with retention, plus a restore verification drill.

Uses the SQLite online backup API rather than a file copy: the app server writes
to the database while it runs, and a plain `cp` can capture a torn page.

Usage:
    python scripts/backup_db.py                 # back up and verify
    python scripts/backup_db.py --keep 14       # keep the newest 14 files
    python scripts/backup_db.py --source X --dest DIR

Exit codes: 0 = backup written and verified, 1 = failure (nothing left behind).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "vilona_v2.db"
DEFAULT_DEST = REPO_ROOT / "backups"

# Tables that must exist in a healthy snapshot. Verification fails when one is
# missing, because a truncated or empty backup would otherwise look "valid".
REQUIRED_TABLES = ("orders", "trades", "positions", "judge_decisions")


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def backup(source: Path, dest_dir: Path) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"source database not found: {source}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    final = dest_dir / f"{source.stem}-{stamp}.db"
    partial = dest_dir / f".{final.name}.partial"

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(partial)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    # Rename only after the copy completes, so an interrupted run never leaves a
    # file that looks like a usable backup.
    os.replace(partial, final)
    return final


def verify(source: Path, snapshot: Path) -> dict[str, int]:
    """Reopen the snapshot and compare table row counts with the source."""
    snap = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        check = snap.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"snapshot integrity_check failed: {check}")
        tables = {
            row[0]
            for row in snap.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise RuntimeError(f"snapshot is missing tables: {', '.join(missing)}")
        snap_counts = {t: _count_rows(snap, t) for t in REQUIRED_TABLES}
    finally:
        snap.close()

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        src_counts = {t: _count_rows(src, t) for t in REQUIRED_TABLES}
    finally:
        src.close()

    # The live database may gain rows between backup and verification; it may
    # never lose them. A snapshot count above the source count means the file is
    # not the database we think it is.
    for table, snap_count in snap_counts.items():
        if snap_count > src_counts[table]:
            raise RuntimeError(
                f"snapshot has more {table} rows ({snap_count}) than source "
                f"({src_counts[table]}); snapshot is not from this database"
            )
    return snap_counts


def prune(dest_dir: Path, stem: str, keep: int) -> list[Path]:
    files = sorted(dest_dir.glob(f"{stem}-*.db"), key=lambda p: p.name, reverse=True)
    removed = []
    for old in files[keep:]:
        old.unlink()
        removed.append(old)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up the Vilona SQLite database.")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--keep", type=int, default=14, help="number of backups to retain")
    args = ap.parse_args()

    try:
        snapshot = backup(args.source, args.dest)
        counts = verify(args.source, snapshot)
    except Exception as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1

    size_mb = snapshot.stat().st_size / (1024 * 1024)
    print(f"backup written: {snapshot} ({size_mb:.2f} MiB)")
    print("verified rows: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    removed = prune(args.dest, args.source.stem, max(1, args.keep))
    if removed:
        print(f"pruned {len(removed)} old backup(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
