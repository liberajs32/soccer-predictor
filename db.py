"""SQLite schema and connection helper shared by the scraper, model, and API.

Two backends, chosen by environment variables:
  - TURSO_DATABASE_URL + TURSO_AUTH_TOKEN set -> Turso (hosted libSQL), used
    in production so scheduled scrapes (GitHub Actions) and the deployed API
    share data that survives redeploys.
  - otherwise -> local data/soccer.db via stdlib sqlite3, as before. This is
    the default so local development needs no Turso account.

Callers (scraper/, model/, api/) only use conn.execute(sql, params).fetchall()
/.fetchone() and conn.commit(), so the Turso branch wraps a small HTTP client
behind that same shape rather than changing any call sites.

The Turso branch talks to Turso's HTTP (Hrana-over-HTTP) API directly with
`requests` instead of the `libsql_client` package: that package's requests
reliably succeeded when run locally but failed every time from GitHub
Actions with an opaque "HTTP status 400" (same credentials, same code --
narrowed down by replaying the same call with plain `requests` and reading
the actual response body, which the library discards). Talking to the
documented v1/execute and v2/pipeline endpoints ourselves sidesteps whatever
that library was doing differently, and is easier to debug if it ever
breaks again since the wire format is fully visible here.
"""
import os
import sqlite3
from pathlib import Path

import requests

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

def _clean_env(name: str) -> str | None:
    """Strip whitespace and any stray non-ASCII characters picked up when
    copy-pasting a secret through a browser/terminal -- both the URL and the
    JWT auth token are pure ASCII by construction, so anything outside that
    range is corruption, not intentional content, and would otherwise crash
    deep inside urllib3 with an opaque UnicodeEncodeError when used as an
    HTTP header value."""
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().encode("ascii", "ignore").decode("ascii")


TURSO_URL = _clean_env("TURSO_DATABASE_URL")
TURSO_TOKEN = _clean_env("TURSO_AUTH_TOKEN")


class _LibsqlCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None


def _to_hrana_value(v):
    """Python value -> Turso's typed-value wire format."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):  # bool is an int subclass -- check first
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _from_hrana_value(cell: dict):
    """Turso's typed-value wire format -> plain Python value."""
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return cell["value"]
    return cell["value"]


class _TursoConnection:
    """Talks to Turso's HTTP API (v1/execute, v2/pipeline) directly and
    looks like sqlite3.Connection for the subset of the API this project
    uses."""

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _stmt(self, sql: str, params) -> dict:
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [_to_hrana_value(p) for p in params]
        return stmt

    def _rows_from_result(self, result: dict) -> list[dict]:
        cols = [c["name"] for c in result["cols"]]
        return [dict(zip(cols, (_from_hrana_value(cell) for cell in row))) for row in result["rows"]]

    def execute(self, sql: str, params=()):
        resp = self._session.post(
            f"{self._base_url}/v1/execute", json={"stmt": self._stmt(sql, params)}, timeout=30
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Turso execute failed ({resp.status_code}): {resp.text}")
        body = resp.json()
        if "result" not in body:
            # Turso can return HTTP 200 with a statement-level error (e.g. a
            # bad SQL query) instead of a result -- surface the actual body
            # instead of letting `["result"]` raise an opaque KeyError.
            raise RuntimeError(f"Turso execute returned no result: {body}")
        return _LibsqlCursor(self._rows_from_result(body["result"]))

    def executescript(self, sql: str) -> None:
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                self.execute(stmt)

    def batch_execute(self, statements: list[tuple[str, tuple]]) -> None:
        """Run many (sql, params) pairs in as few HTTP round-trips as
        possible via Turso's v2/pipeline endpoint. upsert_matches() writes
        thousands of rows, and one HTTP round-trip per row takes minutes;
        batching cuts that to a handful of requests."""
        CHUNK = 200
        for i in range(0, len(statements), CHUNK):
            chunk = statements[i:i + CHUNK]
            body = {
                "requests": [
                    {"type": "execute", "stmt": self._stmt(sql, params)} for sql, params in chunk
                ]
                + [{"type": "close"}]
            }
            resp = self._session.post(f"{self._base_url}/v2/pipeline", json=body, timeout=60)
            if resp.status_code != 200:
                raise RuntimeError(f"Turso batch failed ({resp.status_code}): {resp.text}")
            for item in resp.json()["results"]:
                if item["type"] == "error":
                    raise RuntimeError(f"Turso batch statement failed: {item['error']}")

    def commit(self) -> None:
        pass  # each request commits immediately; there's no open transaction to flush

    def close(self) -> None:
        self._session.close()


def get_connection():
    if TURSO_URL and TURSO_TOKEN:
        # Turso's dashboard hands out "libsql://" URLs; the plain HTTP API
        # (https://) hits the same database.
        http_url = TURSO_URL.replace("libsql://", "https://", 1)
        conn = _TursoConnection(http_url, TURSO_TOKEN)
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
