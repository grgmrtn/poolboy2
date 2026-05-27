"""
fixtures.py — fetch World Cup fixtures from an external API.

Uses the free tier of football-data.org. If no API key is configured,
falls back to realistic mock fixture data so the app still works for
development and demos.

The fixtures are cached in memory for 15 minutes to avoid hammering
the API on every page load.
"""

import os
import time
import uuid
import requests
from database import upsert_fixture, get_fixtures, get_meta, set_meta

# ── Cache ──────────────────────────────────────────────────────────────────
# Simple in-memory cache: stores the last fetch time and the data.
# Resets whenever the app restarts.
_cache = {
    "last_fetched": 0,        # Unix timestamp of the last successful fetch
    "ttl_seconds": 900,       # 15 minutes between refreshes
}


# ── Flag code mapping ──────────────────────────────────────────────────────
# Maps team names (as they appear in the API) to 2-letter ISO country codes.
# flagcdn.com uses these codes to serve flag SVGs, e.g. flagcdn.com/br.svg
FLAG_CODES = {
    "Albania": "al", "Algeria": "dz", "Argentina": "ar", "Australia": "au",
    "Austria": "at",
    "Belgium": "be", "Bolivia": "bo", "Bosnia-Herzegovina": "ba", "Brazil": "br",
    "Canada": "ca", "Cameroon": "cm", "Cape Verde Islands": "cv", "Chile": "cl",
    "Colombia": "co", "Congo DR": "cd", "Costa Rica": "cr", "Croatia": "hr",
    "Curaçao": "cw", "Czech Republic": "cz", "Czechia": "cz",
    "Denmark": "dk",
    "Ecuador": "ec", "Egypt": "eg", "El Salvador": "sv", "England": "gb-eng",
    "France": "fr",
    "Georgia": "ge", "Germany": "de", "Ghana": "gh",
    "Haiti": "ht", "Honduras": "hn", "Hungary": "hu",
    "Indonesia": "id", "Iran": "ir", "Iraq": "iq", "Italy": "it",
    "Ivory Coast": "ci",
    "Japan": "jp", "Jordan": "jo",
    "Mexico": "mx", "Morocco": "ma",
    "Netherlands": "nl", "New Zealand": "nz", "Nigeria": "ng", "Norway": "no",
    "Panama": "pa", "Paraguay": "py", "Peru": "pe", "Poland": "pl",
    "Portugal": "pt",
    "Qatar": "qa",
    "Romania": "ro",
    "Saudi Arabia": "sa", "Scotland": "gb-sct", "Senegal": "sn",
    "Serbia": "rs", "Slovakia": "sk", "Slovenia": "si", "South Africa": "za",
    "South Korea": "kr", "Spain": "es", "Sweden": "se", "Switzerland": "ch",
    "Tunisia": "tn", "Turkey": "tr",
    "Ukraine": "ua", "United States": "us", "Uruguay": "uy", "Uzbekistan": "uz",
    "Venezuela": "ve",
    "Wales": "gb-wls",
    "Zambia": "zm",
}


def get_flag_code(team_name):
    """
    Return the 2-letter flag code for a team name.
    Falls back to "un" (UN flag) if the team isn't in our mapping.
    """
    return FLAG_CODES.get(team_name, "un")


# ── Mock data ──────────────────────────────────────────────────────────────
# WC 2026 group-stage fixtures (draw: 4 Dec 2024). Used when no API key is set.
# Each group plays a full round-robin (3 matchdays, 6 games per group).
_GROUPS = {
    "Group A": ["Argentina", "Chile", "Peru",         "Canada"],
    "Group B": ["Spain",     "Morocco", "Ecuador",    "Dominican Republic"],
    "Group C": ["United States", "Panama", "Honduras","Bosnia-Herzegovina"],
    "Group D": ["France",    "Belgium",  "Italy",     "Algeria"],
    "Group E": ["Brazil",    "Colombia", "Venezuela",  "Mexico"],
    "Group F": ["England",   "Senegal",  "Netherlands","Serbia"],
    "Group G": ["Portugal",  "Germany",  "Cameroon",   "New Zealand"],
    "Group H": ["Spain",     "Turkey",   "Uruguay",    "Czech Republic"],  # placeholder
    "Group I": ["Japan",     "Saudi Arabia", "South Korea", "Ghana"],
    "Group J": ["Croatia",   "Morocco",  "Denmark",    "Mali"],
    "Group K": ["Switzerland","Nigeria", "South Africa","El Salvador"],
    "Group L": ["Australia", "Iran",     "Colombia",   "Zambia"],
}

def _build_mock_fixtures():
    fixtures = []
    base_date = "2026-06-11"
    day_offset = 0
    for group, teams in _GROUPS.items():
        a, b, c, d = teams
        # Matchday 1
        md1_d = f"2026-06-{11 + day_offset:02d}"
        fixtures.append({"home": a, "away": b, "kick_off": f"{md1_d}T18:00:00", "stage": group})
        fixtures.append({"home": c, "away": d, "kick_off": f"{md1_d}T21:00:00", "stage": group})
        day_offset += 1
    day_offset = 0
    for group, teams in _GROUPS.items():
        a, b, c, d = teams
        # Matchday 2
        md2_d = f"2026-06-{23 + day_offset:02d}"
        fixtures.append({"home": a, "away": c, "kick_off": f"{md2_d}T18:00:00", "stage": group})
        fixtures.append({"home": b, "away": d, "kick_off": f"{md2_d}T21:00:00", "stage": group})
        day_offset += 1
    day_offset = 0
    for group, teams in _GROUPS.items():
        a, b, c, d = teams
        # Matchday 3 (simultaneous)
        md3_d = f"2026-07-{4 + day_offset:02d}"
        fixtures.append({"home": a, "away": d, "kick_off": f"{md3_d}T21:00:00", "stage": group})
        fixtures.append({"home": b, "away": c, "kick_off": f"{md3_d}T21:00:00", "stage": group})
        day_offset += 1
    return fixtures

MOCK_FIXTURES = _build_mock_fixtures()


def _load_mock_fixtures():
    """
    Populate the database with mock fixture data.
    
    Each fixture gets a stable UUID generated from its home+away team names
    so it won't change between runs (avoids duplicates).
    """
    import hashlib
    for f in MOCK_FIXTURES:
        # Create a deterministic ID from the matchup so reruns don't duplicate
        stable_id = str(uuid.UUID(hashlib.md5(
            f"{f['home']}-{f['away']}".encode()
        ).hexdigest()))
        upsert_fixture({
            "id":             stable_id,
            "home_team":      f["home"],
            "away_team":      f["away"],
            "home_flag_code": get_flag_code(f["home"]),
            "away_flag_code": get_flag_code(f["away"]),
            "kick_off":       f["kick_off"],
            "stage":          f["stage"],
        })


def _fetch_from_api(api_key):
    """
    Pull World Cup fixtures from football-data.org and save them to the DB.
    
    API docs: https://www.football-data.org/documentation/quickstart
    Free tier gives 10 requests/minute and access to major tournaments.
    
    Returns True on success, False on any error.
    """
    # Competition code 2000 = FIFA World Cup on football-data.org
    url = "https://api.football-data.org/v4/competitions/2000/matches"
    headers = {"X-Auth-Token": api_key}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"[fixtures] API fetch failed: {e}")
        return False

    _STAGE_LABELS = {
        "GROUP_STAGE":   None,          # use group field instead
        "ROUND_OF_32":   "Round of 32",
        "ROUND_OF_16":   "Round of 16",
        "QUARTER_FINALS":"Quarter-Finals",
        "SEMI_FINALS":   "Semi-Finals",
        "THIRD_PLACE":   "Third Place",
        "FINAL":         "Final",
    }

    for match in data.get("matches", []):
        home = match["homeTeam"]["name"] or "TBD"
        away = match["awayTeam"]["name"] or "TBD"

        raw_stage = match.get("stage", "")
        raw_group = match.get("group") or ""
        if raw_group.startswith("GROUP_"):
            stage = "Group " + raw_group[6:]   # GROUP_A → Group A
        else:
            stage = _STAGE_LABELS.get(raw_stage) or raw_stage.replace("_", " ").title()

        upsert_fixture({
            "id":             str(match["id"]),
            "home_team":      home,
            "away_team":      away,
            "home_flag_code": get_flag_code(home),
            "away_flag_code": get_flag_code(away),
            "kick_off":       match.get("utcDate", ""),
            "stage":          stage,
        })

    print(f"[fixtures] Synced {len(data.get('matches', []))} fixtures from API")
    return True


def sync_fixtures():
    """
    Refresh fixtures if the TTL has expired.

    Check order (fastest to slowest):
    1. In-memory cache — zero overhead, resets on process restart.
    2. Database meta row — survives redeploys and is shared across instances.
    3. API / mock — only called when both caches are stale.
    """
    now = time.time()
    ttl = _cache["ttl_seconds"]

    # Fast path: in-memory cache still warm.
    if now - _cache["last_fetched"] < ttl:
        return

    # DB path: check persistent timestamp (survives redeploys).
    try:
        db_ts = float(get_meta("fixtures_last_fetched") or 0)
    except (TypeError, ValueError):
        db_ts = 0

    if now - db_ts < ttl:
        _cache["last_fetched"] = db_ts  # warm the in-memory cache
        return

    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if api_key:
        success = _fetch_from_api(api_key)
        if success:
            _cache["last_fetched"] = now
            set_meta("fixtures_last_fetched", str(now))
    else:
        _load_mock_fixtures()
        _cache["last_fetched"] = now
        set_meta("fixtures_last_fetched", str(now))
        print("[fixtures] No API key found; using mock fixture data.")


_STAGE_ORDER = [
    "Group A", "Group B", "Group C", "Group D", "Group E", "Group F",
    "Group G", "Group H", "Group I", "Group J", "Group K", "Group L",
    "Round of 32", "Round of 16", "Quarter-Finals", "Semi-Finals",
    "Third Place", "Final",
]

def _stage_key(name):
    try:
        return _STAGE_ORDER.index(name)
    except ValueError:
        return 999


def get_all_fixtures():
    """
    Return all fixtures from the database, grouped by stage in tournament order.
    Triggers a cache refresh if needed before returning.
    """
    sync_fixtures()
    rows = get_fixtures()

    grouped = {}
    for row in rows:
        stage = row["stage"] or "Other"
        if stage not in grouped:
            grouped[stage] = []
        grouped[stage].append(dict(row))

    return dict(sorted(grouped.items(), key=lambda kv: _stage_key(kv[0])))
