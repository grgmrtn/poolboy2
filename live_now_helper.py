"""
live_now_helper.py — server-side ESPN live-data fetch with a short
in-memory TTL cache.

Hits ESPN's public scoreboard (no auth) and caches the response for 30s
so concurrent requests don't pile up on the API. The Flask app uses
this from two places:

  * pool_page render — augments grouped_fixtures in-memory so the live
    banner has fresh data on initial paint
  * /pool/<id>/live-now JSON endpoint — same augmentation for the
    30-second client poller

Because the fetch is on-demand, we no longer depend on a fast scheduled
cron to keep live_* columns up to date. The background auto-score cron
still settles FINISHED matches, but its cadence is no longer felt by
users staring at the banner.
"""
import os
import re
import time
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

def _make_resilient_session():
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

_HTTP = _make_resilient_session()
# Short TTL because Railway runs the web service in multiple containers, each
# with its own in-memory cache. A long TTL meant the user's round-robin polls
# could see "score → goal → score back" flicker when two containers had
# different cached snapshots. 5s is plenty short to mask that, and ESPN's
# public CDN absorbs the load (a few thousand requests/day is trivial).
_TTL_SECONDS = 5

# (timestamp, list[event]) — module-global cache; safe under multiple
# concurrent Flask requests because GIL serialises the dict update.
_CACHE = {"ts": 0.0, "events": []}

# ESPN ↔ football-data team-name aliases. Add more as we trip over
# them during the tournament.
ESPN_TEAM_ALIASES = {
    "USA":              "United States",
    "Korea Republic":   "South Korea",
    "IR Iran":          "Iran",
    "Republic of Ireland": "Ireland",
    "Czechia":          "Czech Republic",
}


def _normalise(name):
    if not name:
        return ""
    s = str(name).strip()
    return ESPN_TEAM_ALIASES.get(s, s)


def _parse_minute(display_clock):
    """Turn ESPN displayClock to integer minute (0-120, or None)."""
    if not display_clock:
        return None
    s = str(display_clock).strip()
    if s in ("HT", "Halftime"):
        return 45
    if s in ("FT", "Full Time", "End"):
        return 90
    m = re.match(r"(\d+)", s)
    if m:
        try:
            return min(120, int(m.group(1)))
        except ValueError:
            pass
    return None


def get_espn_live():
    """Return a list of normalised live-event dicts. Cached for 30s."""
    now = time.time()
    if (now - _CACHE["ts"]) < _TTL_SECONDS and _CACHE["events"]:
        return _CACHE["events"]
    out = []
    try:
        r = _HTTP.get(ESPN_URL, timeout=8)
        if r.status_code == 200:
            data = r.json()
            for evt in data.get("events", []):
                status = evt.get("status") or {}
                state  = (status.get("type") or {}).get("state", "")
                if state not in ("in", "live"):
                    continue
                comp = (evt.get("competitions") or [{}])[0]
                comps = comp.get("competitors", [])
                if len(comps) != 2:
                    continue
                home = next((c for c in comps if c.get("homeAway") == "home"), None)
                away = next((c for c in comps if c.get("homeAway") == "away"), None)
                if not home or not away:
                    continue
                try:
                    h_score = int(home.get("score") or 0)
                    a_score = int(away.get("score") or 0)
                except (ValueError, TypeError):
                    continue
                venue = comp.get("venue") or {}
                city  = (venue.get("address") or {}).get("city")
                out.append({
                    "home_team":  _normalise(home.get("team", {}).get("displayName")),
                    "away_team":  _normalise(away.get("team", {}).get("displayName")),
                    "home_score": h_score,
                    "away_score": a_score,
                    "minute":     _parse_minute(status.get("displayClock")),
                    "status":     "IN_PLAY",
                    "city":       city,
                    "updated_at": evt.get("date") or "",
                })
    except Exception as e:
        # Surface to logs but don't break the page. Cache an empty list
        # for a short while so a flapping API doesn't get hammered.
        print(f"[live_now_helper] ESPN fetch failed: {e}")
    _CACHE["ts"] = now
    _CACHE["events"] = out
    return out


def by_team_pair(events):
    """Index events for O(1) lookup by (home_team, away_team)."""
    return {(e["home_team"], e["away_team"]): e for e in events}


def augment_fixtures_with_espn(fixtures_iter):
    """
    Walk an iterable of fixture dicts and stamp ESPN's live data onto
    any matching fixture (mutates in place). Returns the set of fixture
    ids that got ESPN data, so callers can know which fixtures are
    "live according to ESPN" without re-walking.
    """
    espn = by_team_pair(get_espn_live())
    if not espn:
        return set()
    touched = set()
    for f in fixtures_iter:
        key = (f.get("home_team"), f.get("away_team"))
        e = espn.get(key)
        if not e:
            continue
        f["live_home_score"] = e["home_score"]
        f["live_away_score"] = e["away_score"]
        f["live_status"]     = e["status"]
        f["live_minute"]     = e["minute"]
        # Don't clobber a city we already had if ESPN doesn't supply one
        if e.get("city"):
            f["city"] = e["city"]
        touched.add(f.get("id"))
    return touched
