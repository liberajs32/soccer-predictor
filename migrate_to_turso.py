"""One-time migration: copy the local data/soccer.db into Turso.

Run this once after creating a Turso database, with TURSO_DATABASE_URL and
TURSO_AUTH_TOKEN set in the environment (get_connection() in db.py then
returns a Turso connection instead of the local sqlite3 one -- everything
else about upsert_matches() stays the same).

Usage (PowerShell):
    $env:TURSO_DATABASE_URL = "libsql://..."
    $env:TURSO_AUTH_TOKEN = "..."
    python migrate_to_turso.py
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from db import TURSO_TOKEN, TURSO_URL, get_connection, upsert_matches

LOCAL_DB_PATH = Path(__file__).resolve().parent / "data" / "soccer.db"


@dataclass
class _Row:
    league: str
    season: str
    round: str | None
    date: str | None
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None
    odds_home: float | None
    odds_draw: float | None
    odds_away: float | None
    source_url: str


def main() -> None:
    if not (TURSO_URL and TURSO_TOKEN):
        raise SystemExit("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first (see this file's docstring).")
    if not LOCAL_DB_PATH.exists():
        raise SystemExit(f"No local database at {LOCAL_DB_PATH} to migrate from.")

    local = sqlite3.connect(LOCAL_DB_PATH)
    local.row_factory = sqlite3.Row
    rows = local.execute(
        """
        SELECT league, season, round, date, home_team, away_team,
               home_score, away_score, odds_home, odds_draw, odds_away, source_url
        FROM matches
        """
    ).fetchall()
    records = [_Row(**dict(r)) for r in rows]
    print(f"Read {len(records)} rows from {LOCAL_DB_PATH}")

    turso_conn = get_connection()
    written = upsert_matches(turso_conn, records)
    print(f"Wrote {written} rows to Turso ({TURSO_URL})")
    turso_conn.close()


if __name__ == "__main__":
    main()
