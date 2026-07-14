"""SQLite schema and connection helper shared by the scraper, model, and API.

Two backends, chosen by environment variables:
  - TURSO_DATABASE_URL + TURSO_AUTH_TOKEN set -> Turso (hosted libSQL), used
    in production so scheduled scrapes (GitHub Actions) and the deployed API
    share data that survives redeploys.
  - otherwise -> local data/soccer.db via stdlib sqlite3, as before. This is
    the default so local development needs no Turso account.

Callers (scraper/, model/, api/) only use conn.execute(sql, params).fetchall()
/.fetchone() and conn.commit(), so the Turso branch wraps libsql_client's
ClientSync behind that same shape rather than changing any call sites.
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "soccer.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league TEXT NOT NULL,
    season TEXT NOT NULL,
    round TEXT,
    date TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    odds_home REAL,
    odds_draw REAL,
    odds_away REAL,
    source_url TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(league, season, home_team, away_team, date)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    model_version TEXT NOT NULL,
    prob_home REAL NOT NULL,
    prob_draw REAL NOT NULL,
    prob_away REAL NOT NULL,
    predicted_outcome TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")


class _LibsqlCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _LibsqlConnection:
    """Makes libsql_client.ClientSync look like sqlite3.Connection for the
    subset of the API this project uses."""

    def __init__(self, client):
        self._client = client

    def execute(self, sql: str, params=()):
        result = self._client.execute(sql, list(params) if params else None)
        rows = [dict(zip(result.columns, row)) for row in result.rows]
        return _LibsqlCursor(rows)

    def executescript(self, sql: str) -> None:
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                self._client.execute(stmt)

    def batch_execute(self, statements: list[tuple[str, tuple]]) -> None:
        """Run many (sql, params) pairs in as few HTTP round-trips as
        possible. upsert_matches() writes thousands of rows one at a time
        over Turso's HTTP API each execute() is its own request, so a plain
        per-row loop takes minutes; batching cuts that to a handful of
        requests."""
        import libsql_client

        CHUNK = 200
        stmts = [libsql_client.Statement(sql, list(params) if params else None) for sql, params in statements]
        for i in range(0, len(stmts), CHUNK):
            self._client.batch(stmts[i:i + CHUNK])

    def commit(self) -> None:
        pass  # libsql_client commits each standalone execute() immediately

    def close(self) -> None:
        self._client.close()


def get_connection():
    if TURSO_URL and TURSO_TOKEN:
        import libsql_client

        # Turso's dashboard hands out "libsql://" URLs, which libsql_client
        # opens as a websocket (wss://) connection -- that handshake fails
        # in some sandboxed/CI network environments. The plain HTTP API
        # (https://) hits the same database and works everywhere, so swap
        # the scheme rather than asking users to edit the URL themselves.
        http_url = TURSO_URL.replace("libsql://", "https://", 1)
        client = libsql_client.create_client_sync(url=http_url, auth_token=TURSO_TOKEN)
        conn = _LibsqlConnection(client)
        conn.executescript(SCHEMA)
        return conn

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


_UPSERT_SQL = """
INSERT INTO matches
    (league, season, round, date, home_team, away_team,
     home_score, away_score, odds_home, odds_draw, odds_away, source_url)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(league, season, home_team, away_team, date) DO UPDATE SET
    round=excluded.round,
    home_score=excluded.home_score,
    away_score=excluded.away_score,
    odds_home=excluded.odds_home,
    odds_draw=excluded.odds_draw,
    odds_away=excluded.odds_away,
    source_url=excluded.source_url,
    scraped_at=datetime('now')
"""


def upsert_matches(conn, records) -> int:
    """Insert MatchRecord-like objects, updating scores/odds on conflict. Returns rows written."""
    params_list = [
        (
            r.league, r.season, r.round, r.date, r.home_team, r.away_team,
            r.home_score, r.away_score, r.odds_home, r.odds_draw, r.odds_away,
            r.source_url,
        )
        for r in records
    ]

    if hasattr(conn, "batch_execute"):
        conn.batch_execute([(_UPSERT_SQL, params) for params in params_list])
        return len(params_list)

    for params in params_list:
        conn.execute(_UPSERT_SQL, params)
    conn.commit()
    return len(params_list)
