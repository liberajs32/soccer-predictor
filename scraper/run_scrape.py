"""CLI: scrape one or more league/season combos from BetExplorer into SQLite.

Usage:
    python scraper/run_scrape.py --league EPL --season 2025-2026
    python scraper/run_scrape.py --league EPL K1 K2 BL1 --seasons-back 5
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betexplorer import CALENDAR_YEAR_LEAGUES, LEAGUE_URL_SEGMENTS, fetch_fixtures, fetch_season
from db import get_connection, upsert_matches


def recent_seasons(n: int, end_year: int, league: str) -> list[str]:
    if league in CALENDAR_YEAR_LEAGUES:
        return [str(end_year - i) for i in range(n)]
    return [f"{end_year - i}-{end_year - i + 1}" for i in range(n)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", nargs="+", choices=sorted(LEAGUE_URL_SEGMENTS), required=True)
    parser.add_argument("--season", help='e.g. "2025-2026" or "current"')
    parser.add_argument("--seasons-back", type=int, help="scrape this many seasons ending at --end-year")
    parser.add_argument("--end-year", type=int, default=2025, help="most recent season's start year")
    parser.add_argument("--no-cache", action="store_true", help="bypass the on-disk HTML cache")
    parser.add_argument("--fixtures", action="store_true", help="scrape upcoming (unplayed) fixtures instead of results")
    args = parser.parse_args()

    conn = get_connection()
    total = 0

    if args.fixtures:
        for league in args.league:
            records = fetch_fixtures(league, use_cache=not args.no_cache)
            written = upsert_matches(conn, records)
            total += written
            print(f"{league} fixtures: {written} matches")
        print(f"Total: {total} matches written to {Path('data/soccer.db').resolve()}")
        return

    for league in args.league:
        if args.season:
            seasons = [args.season]
        elif args.seasons_back:
            seasons = recent_seasons(args.seasons_back, args.end_year, league)
        else:
            seasons = ["current"]

        for season in seasons:
            records = fetch_season(league, season, use_cache=not args.no_cache)
            written = upsert_matches(conn, records)
            total += written
            print(f"{league} {season}: {written} matches")
    print(f"Total: {total} matches written to {Path('data/soccer.db').resolve()}")


if __name__ == "__main__":
    main()
