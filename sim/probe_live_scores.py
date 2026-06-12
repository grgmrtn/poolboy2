"""
sim/probe_live_scores.py — diagnostic: hit football-data.org and dump every
field on any IN_PLAY / PAUSED / LIVE match. Tells us whether the free tier
gives running scores + minutes for live games, or whether we need a paid
tier / different source.

Usage:
    FOOTBALL_DATA_API_KEY='...' python3 sim/probe_live_scores.py
"""
import os
import sys
import json
import requests

URL = "https://api.football-data.org/v4/competitions/2000/matches"
LIVE_STATUSES = {"IN_PLAY", "PAUSED", "LIVE"}

# ESPN public scoreboards we'll cross-check against. The slug
# `fifa.world` is the WC; we try a couple of variants since ESPN has
# occasionally renamed the path.
ESPN_URLS = [
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.worldcup/scoreboard",
]


def main():
    key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set"); sys.exit(1)

    r = requests.get(URL, headers={"X-Auth-Token": key}, timeout=20)
    if r.status_code != 200:
        print(f"API error {r.status_code}: {r.text[:400]}"); sys.exit(1)

    matches = r.json().get("matches", [])
    live    = [m for m in matches if m.get("status") in LIVE_STATUSES]
    by_status = {}
    for m in matches:
        s = m.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1

    print(f"competition: 2000 (FIFA World Cup)")
    print(f"matches returned: {len(matches)}")
    print(f"status counts: {by_status}\n")

    if not live:
        print("No IN_PLAY / PAUSED / LIVE matches right now.")
        print("Re-run during a live match to capture the field set.")
        return

    print(f"━━ {len(live)} LIVE MATCH(es) ━━\n")
    for m in live:
        print(f"  {m['homeTeam']['name']}  vs  {m['awayTeam']['name']}")
        print(f"    id:          {m['id']}")
        print(f"    status:      {m.get('status')}")
        print(f"    minute:      {m.get('minute')}")       # may be None on free tier
        print(f"    injuryTime:  {m.get('injuryTime')}")
        print(f"    score:")
        print(json.dumps(m.get("score") or {}, indent=8))
        print(f"    lastUpdated: {m.get('lastUpdated')}")
        print()

    # ── ESPN cross-check ─────────────────────────────────────────────
    print("━━ ESPN cross-check (independent source) ━━\n")
    espn_data = None
    for url in ESPN_URLS:
        try:
            er = requests.get(url, timeout=10)
            if er.status_code == 200 and er.json().get("events"):
                espn_data = er.json()
                print(f"hit: {url}")
                break
        except Exception:
            continue
    if not espn_data:
        print("ESPN endpoint not reachable on the tried slugs — skip.")
        return

    for evt in espn_data.get("events", []):
        status_obj = evt.get("status") or {}
        st = (status_obj.get("type") or {}).get("state", "")
        if st not in ("in", "live"):
            continue
        comps = (evt.get("competitions") or [{}])[0].get("competitors", [])
        if len(comps) != 2: continue
        home = next((c for c in comps if c.get("homeAway") == "home"), comps[0])
        away = next((c for c in comps if c.get("homeAway") == "away"), comps[1])

        clock     = status_obj.get("displayClock", "?")
        period    = status_obj.get("period")            # 1 = 1st half, 2 = 2nd
        period_lbl = (status_obj.get("type") or {}).get("shortDetail", "")
        print(f"  {home['team']['displayName']}  {home.get('score','?')} – "
              f"{away.get('score','?')}  {away['team']['displayName']}")
        print(f"    displayClock: {clock!r}    period: {period!r}    detail: {period_lbl!r}")
    print()
    print("━━ verdict ━━")
    print("Compare ESPN's score against football-data's score.fullTime above.")
    print("- Match? → football-data IS giving live scores; wire it up.")
    print("- ESPN > 0 but football-data still 0-0? → football-data only")
    print("  reports at FINISHED on this competition; use ESPN instead.")


if __name__ == "__main__":
    main()
