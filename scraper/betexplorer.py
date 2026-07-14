"""Parse BetExplorer season-results pages into match records.

Page structure observed on betexplorer.com (results page for a given
league+season), inside <div id="js-leagueresults-all">:

    <table class="table-main ...">
      <tr><th colspan="2">38. Round</th><th>1</th><th>X</th><th>2</th><th></th></tr>
      <tr>
        <td class="h-text-left"><a class="in-match"><span>Home</span> - <span>Away</span></a></td>
        <td class="h-text-center"><a>0:3</a></td>
        <td class="table-main__odds" data-odd="1.90">1.90</td>
        <td class="table-main__odds" data-odd="4.28">4.28</td>
        <td class="table-main__odds colored"><span><span><span data-odd="3.49">3.49</span></span></span></td>
        <td class="h-text-right h-text-no-wrap">25.05.</td>
      </tr>
      ...
    </table>

Round-header rows (a <th colspan> cell) are skipped; everything else with
6 <td> cells is treated as a match row.

The odds cells carry their value in a `data-odd="1.90"` attribute (on the
<td> itself, or on a nested <span> for the "colored"/favorite column) rather
than as visible text -- the site fills in display text client-side via JS,
which a plain `requests` fetch never runs, so cell.get_text() comes back
empty for odds.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from fetch import get_html

LEAGUE_URL_SEGMENTS = {
    "EPL": "england/premier-league",
    "K1": "south-korea/k-league-1",
    "K2": "south-korea/k-league-2",
    "BL1": "germany/bundesliga",
}

# EPL/BL1 run Aug-May and are named "2025-2026"; K1/K2 run Feb-Dec on a
# single calendar year and are named just "2025" (confirmed via the site's
# season <select> options for each league).
CALENDAR_YEAR_LEAGUES = {"K1", "K2"}

BASE_URL = "https://www.betexplorer.com/football"


@dataclass
class MatchRecord:
    league: str
    season: str
    round: Optional[str]
    date: Optional[str]  # ISO yyyy-mm-dd, None if undetermined
    home_team: str
    away_team: str
    home_score: Optional[int]
    away_score: Optional[int]
    odds_home: Optional[float]
    odds_draw: Optional[float]
    odds_away: Optional[float]
    source_url: str


def season_results_url(league: str, season: str) -> str:
    """season format: "2025-2026" for split-year leagues (EPL/BL1), or a
    single "2025" for calendar-year leagues (K1/K2). "current" uses the
    no-suffix URL for the league's in-progress season."""
    segment = LEAGUE_URL_SEGMENTS[league]
    league_name = segment.split("/")[-1]
    country = segment.split("/")[0]
    if season == "current":
        return f"{BASE_URL}/{country}/{league_name}/results/"
    return f"{BASE_URL}/{country}/{league_name}-{season}/results/"


def _parse_float(text: str) -> Optional[float]:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None


def _extract_odd(cell) -> Optional[float]:
    """Read an odds value from `data-odd`, checking the cell itself and any
    descendant (the "colored"/favorite column nests it in a <span>)."""
    node = cell if cell.has_attr("data-odd") else cell.find(attrs={"data-odd": True})
    if node is None:
        return None
    return _parse_float(node["data-odd"])


def _parse_score(text: str) -> tuple[Optional[int], Optional[int]]:
    match = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _resolve_date(day_month: str, season: str, league: str) -> Optional[str]:
    """"25.05." + season "2025-2026" -> "2026-05-25" (Aug-Dec -> first year,
    Jan-Jul -> second year). For calendar-year leagues (K1/K2), season "2025"
    applies directly since the whole season falls in one calendar year."""
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\.", day_month.strip())
    if not match or season == "current":
        return None
    day, month = int(match.group(1)), int(match.group(2))
    if league in CALENDAR_YEAR_LEAGUES:
        year = int(season)
    else:
        yr_from, yr_to = (int(y) for y in season.split("-"))
        year = yr_from if month >= 7 else yr_to
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_results_page(html: str, league: str, season: str, source_url: str) -> list[MatchRecord]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="js-leagueresults-all")
    if container is None:
        return []

    table = container.find("table", class_="table-main")
    if table is None:
        return []

    records: list[MatchRecord] = []
    current_round: Optional[str] = None

    for row in table.find_all("tr"):
        header_cell = row.find("th", attrs={"colspan": True})
        if header_cell is not None:
            current_round = header_cell.get_text(strip=True)
            continue

        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        team_link = cells[0].find("a")
        if team_link is None:
            continue
        team_text = team_link.get_text(" ", strip=True)
        if " - " not in team_text:
            continue
        home_team, away_team = (t.strip() for t in team_text.split(" - ", 1))

        home_score, away_score = _parse_score(cells[1].get_text(strip=True))

        odds_home = _extract_odd(cells[2])
        odds_draw = _extract_odd(cells[3])
        odds_away = _extract_odd(cells[4])

        iso_date = _resolve_date(cells[5].get_text(strip=True), season, league)

        records.append(
            MatchRecord(
                league=league,
                season=season,
                round=current_round,
                date=iso_date,
                home_team=home_team,
                away_team=away_team,
                home_score=home_score,
                away_score=away_score,
                odds_home=odds_home,
                odds_draw=odds_draw,
                odds_away=odds_away,
                source_url=source_url,
            )
        )

    return records


def _find_stage_urls(html: str, base_url: str) -> list[str]:
    """K League 1 splits finished seasons into stages (Main / Championship
    Group / Relegation Group, ...) via a secondary tab bar:

        <ul class="list-tabs list-tabs--secondary">
          <li><a href="?stage=pSZ0WVeb">Main</a></li>
          <li><a href="?stage=OIY4VkB4" class="current">Championship Group</a></li>
          ...
        </ul>

    The un-suffixed results URL only renders whichever stage happens to be
    marked "current" (observed: the *last* stage of the season, e.g. a
    15-match Championship Group decider) rather than the full season, so a
    season with this tab bar has to be fetched once per stage and the
    results merged. Leagues without a split (EPL, BL1, K2, and any
    in-progress season before its split happens) simply have no such tab
    bar, and this returns an empty list.
    """
    soup = BeautifulSoup(html, "html.parser")
    tabs = soup.find("ul", class_="list-tabs--secondary")
    if tabs is None:
        return []
    urls = []
    for link in tabs.find_all("a", href=True):
        urls.append(urljoin(base_url, link["href"]))
    return urls


def fetch_season(league: str, season: str, use_cache: bool = True) -> list[MatchRecord]:
    url = season_results_url(league, season)
    html = get_html(url, use_cache=use_cache)
    records = parse_results_page(html, league, season, url)

    for stage_url in _find_stage_urls(html, url):
        if stage_url == url:
            continue
        stage_html = get_html(stage_url, use_cache=use_cache)
        records.extend(parse_results_page(stage_html, league, season, stage_url))

    return records


# --- Fixtures (upcoming, unplayed matches) ---
#
# The /fixtures/ page uses a different row layout than /results/, inside
# <div id="js-leaguefixtures-all">:
#
#   <tr><th colspan="2">18. Round</th><th></th><th>B's</th><th>1</th><th>X</th><th>2</th></tr>
#   <tr>
#     <td class="table-main__datetime">18.07. 12:30</td>   <!-- blank ("&nbsp;") if same as the row above -->
#     <td class="h-text-left"><a class="in-match"><span>Home</span> - <span>Away</span></a></td>
#     <td class="h-text-center"></td>                       <!-- always empty: not played yet -->
#     <td class="table-main__bs">10</td>                    <!-- bookmaker count, unused -->
#     <td class="table-main__odds"><button data-odd="2.37">2.37</button></td>
#     <td class="table-main__odds"><button data-odd="3.35">3.35</button></td>
#     <td class="table-main__odds"><button data-odd="2.82">2.82</button></td>
#   </tr>
#
# Odds again live in a `data-odd` attribute (on the nested <button> here),
# handled by the same _extract_odd() helper used for the results page.

def fixtures_url(league: str) -> str:
    segment = LEAGUE_URL_SEGMENTS[league]
    return f"{BASE_URL}/{segment}/fixtures/"


def _resolve_fixture_date(datetime_text: str, reference: date) -> Optional[str]:
    """"18.07. 12:30" (no year) -> "2026-07-18", rolling over to next year if
    the month has already passed relative to `reference` (today)."""
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\.", datetime_text.strip())
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year = reference.year
    if (month, day) < (reference.month, reference.day):
        year += 1
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_fixtures_page(html: str, league: str, source_url: str, reference_date: Optional[date] = None) -> list[MatchRecord]:
    reference_date = reference_date or datetime.now().date()
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="js-leaguefixtures-all")
    if container is None:
        return []

    table = container.find("table", class_="table-main")
    if table is None:
        return []

    records: list[MatchRecord] = []
    current_round: Optional[str] = None
    last_date_text: Optional[str] = None

    for row in table.find_all("tr"):
        header_cell = row.find("th", attrs={"colspan": True})
        if header_cell is not None:
            current_round = header_cell.get_text(strip=True)
            last_date_text = None
            continue

        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        team_link = cells[1].find("a")
        if team_link is None:
            continue
        team_text = team_link.get_text(" ", strip=True)
        if " - " not in team_text:
            continue
        home_team, away_team = (t.strip() for t in team_text.split(" - ", 1))

        date_text = cells[0].get_text(strip=True)
        if date_text:
            last_date_text = date_text
        iso_date = _resolve_fixture_date(last_date_text, reference_date) if last_date_text else None

        odds_home = _extract_odd(cells[4])
        odds_draw = _extract_odd(cells[5])
        odds_away = _extract_odd(cells[6])

        records.append(
            MatchRecord(
                league=league,
                season="current",
                round=current_round,
                date=iso_date,
                home_team=home_team,
                away_team=away_team,
                home_score=None,
                away_score=None,
                odds_home=odds_home,
                odds_draw=odds_draw,
                odds_away=odds_away,
                source_url=source_url,
            )
        )

    return records


def fetch_fixtures(league: str, use_cache: bool = True) -> list[MatchRecord]:
    url = fixtures_url(league)
    html = get_html(url, use_cache=use_cache)
    return parse_fixtures_page(html, league, url)
