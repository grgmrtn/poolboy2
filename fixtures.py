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
from database import upsert_fixture, get_fixtures

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
    "Brazil": "br", "Germany": "de", "France": "fr", "Argentina": "ar",
    "Spain": "es", "England": "gb-eng", "Portugal": "pt", "Netherlands": "nl",
    "Belgium": "be", "Uruguay": "uy", "Croatia": "hr", "Morocco": "ma",
    "Japan": "jp", "Senegal": "sn", "United States": "us", "Mexico": "mx",
    "Switzerland": "ch", "Australia": "au", "South Korea": "kr", "Ghana": "gh",
    "Denmark": "dk", "Poland": "pl", "Ecuador": "ec", "Qatar": "qa",
    "Canada": "ca", "Cameroon": "cm", "Serbia": "rs", "Wales": "gb-wls",
    "Costa Rica": "cr", "Tunisia": "tn", "Saudi Arabia": "sa", "Iran": "ir",
    "Italy": "it", "Colombia": "co", "Chile": "cl", "Nigeria": "ng",
    "Egypt": "eg", "Ivory Coast": "ci", "Turkey": "tr", "Ukraine": "ua",
    "Austria": "at", "Sweden": "se", "Czech Republic": "cz", "Hungary": "hu",
    "Slovakia": "sk", "Slovenia": "si", "Scotland": "gb-sct", "Albania": "al",
    "Romania": "ro", "Georgia": "ge",
}


def get_flag_code(team_name):
    """
    Return the 2-letter flag code for a team name.
    Falls back to "un" (UN flag) if the team isn't in our mapping.
    """
    return FLAG_CODES.get(team_name, "un")


# ── Mock data ──────────────────────────────────────────────────────────────
# Realistic World Cup 2026 group-stage fixtures for development/demo.
# In production, these are replaced by live API data.
MOCK_FIXTURES = [
    {"home": "Brazil",        "away": "Germany",      "kick_off": "2026-06-11T18:00:00", "stage": "Group A"},
    {"home": "France",        "away": "Argentina",    "kick_off": "2026-06-11T21:00:00", "stage": "Group B"},
    {"home": "Spain",         "away": "England",      "kick_off": "2026-06-12T18:00:00", "stage": "Group C"},
    {"home": "Portugal",      "away": "Netherlands",  "kick_off": "2026-06-12T21:00:00", "stage": "Group D"},
    {"home": "Belgium",       "away": "Uruguay",      "kick_off": "2026-06-13T18:00:00", "stage": "Group E"},
    {"home": "Croatia",       "away": "Morocco",      "kick_off": "2026-06-13T21:00:00", "stage": "Group F"},
    {"home": "Japan",         "away": "Senegal",      "kick_off": "2026-06-14T18:00:00", "stage": "Group G"},
    {"home": "United States", "away": "Mexico",       "kick_off": "2026-06-14T21:00:00", "stage": "Group H"},
    {"home": "Germany",       "away": "France",       "kick_off": "2026-06-15T18:00:00", "stage": "Group A"},
    {"home": "Brazil",        "away": "Argentina",    "kick_off": "2026-06-15T21:00:00", "stage": "Group B"},
    {"home": "England",       "away": "Portugal",     "kick_off": "2026-06-16T18:00:00", "stage": "Group C"},
    {"home": "Spain",         "away": "Netherlands",  "kick_off": "2026-06-16T21:00:00", "stage": "Group D"},
    {"home": "Morocco",       "away": "Belgium",      "kick_off": "2026-06-17T18:00:00", "stage": "Group E"},
    {"home": "Uruguay",       "away": "Croatia",      "kick_off": "2026-06-17T21:00:00", "stage": "Group F"},
    {"home": "Senegal",       "away": "United States","kick_off": "2026-06-18T18:00:00", "stage": "Group G"},
    {"home": "Mexico",        "away": "Japan",        "kick_off": "2026-06-18T21:00:00", "stage": "Group H"},
    {"home": "Argentina",     "away": "Germany",      "kick_off": "2026-06-19T18:00:00", "stage": "Group A"},
    {"home": "France",        "away": "Brazil",       "kick_off": "2026-06-19T21:00:00", "stage": "Group B"},
    {"home": "Netherlands",   "away": "England",      "kick_off": "2026-06-20T18:00:00", "stage": "Group C"},
    {"home": "Portugal",      "away": "Spain",        "kick_off": "2026-06-20T21:00:00", "stage": "Group D"},
    {"home": "Croatia",       "away": "Belgium",      "kick_off": "2026-06-21T18:00:00", "stage": "Group E"},
    {"home": "Uruguay",       "away": "Morocco",      "kick_off": "2026-06-21T21:00:00", "stage": "Group F"},
    {"home": "United States", "away": "Japan",        "kick_off": "2026-06-22T18:00:00", "stage": "Group G"},
    {"home": "Mexico",        "away": "Senegal",      "kick_off": "2026-06-22T21:00:00", "stage": "Group H"},
]


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

    for match in data.get("matches", []):
        home = match["homeTeam"]["name"]
        away = match["awayTeam"]["name"]
        fixture = {
            "id":             str(match["id"]),
            "home_team":      home,
            "away_team":      away,
            "home_flag_code": get_flag_code(home),
            "away_flag_code": get_flag_code(away),
            "kick_off":       match.get("utcDate", ""),
            "stage":          match.get("stage", ""),
        }
        fixture["home_team"] = fixture["home_team"] or "TBD"
        fixture["away_team"] = fixture["away_team"] or "TBD"
        
        upsert_fixture(fixture)

    print(f"[fixtures] Synced {len(data.get('matches', []))} fixtures from API")
    return True


def sync_fixtures():
    """
    Main entry point: refresh fixtures if the cache has expired.
    
    Priority:
    1. If FOOTBALL_DATA_API_KEY env var is set, fetch from the live API.
    2. Otherwise, load mock data (great for development).
    
    The cache TTL prevents this from making an API call on every page load.
    'api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    """
    now = time.time()
    elapsed = now - _cache["last_fetched"]

    if elapsed < _cache["ttl_seconds"]:
        return  # Cache is still fresh, nothing to do

    
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")

    if api_key:
        success = _fetch_from_api(api_key)
        if success:
            _cache["last_fetched"] = now
    else:
        # No API key — use mock data
        _load_mock_fixtures()
        _cache["last_fetched"] = now
        print("[fixtures] No API key found; using mock fixture data.")


def get_all_fixtures():
    """
    Return all fixtures from the database, grouped by stage.
    
    Triggers a cache refresh if needed before returning.
    
    Returns: dict like {"Group A": [fixture, ...], "Group B": [...], ...}
    """
    sync_fixtures()
    rows = get_fixtures()

    grouped = {}
    for row in rows:
        stage = row["stage"] or "Other"
        if stage not in grouped:
            grouped[stage] = []
        grouped[stage].append(dict(row))

    return grouped
