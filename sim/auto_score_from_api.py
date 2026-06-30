"""
sim/auto_score_from_api.py — pull fixture status from football-data.org
and (a) settle FINISHED matches and (b) stash running live scores for
matches still IN_PLAY / PAUSED.

For each match returned by the API:
  - FINISHED, not yet scored in our DB → UPDATE home_score / away_score /
    result → process_fixture_result(fid) to credit payouts. Also clears
    any previously-set live_* columns since the final score takes over.
  - IN_PLAY / PAUSED → write live_home_score / live_away_score / live_status
    / live_updated_at so the pool pill can render the running score in big
    numbers without each user hitting the API.
  - FINISHED and already scored → if it still has live_* columns set from
    an earlier IN_PLAY poll, wipe them.

Idempotent. Default is DRY-RUN (no writes); add --apply to commit.

Usage:
    DATABASE_URL='...' FOOTBALL_DATA_API_KEY='...' \\
        python3 sim/auto_score_from_api.py
    DATABASE_URL='...' FOOTBALL_DATA_API_KEY='...' \\
        python3 sim/auto_score_from_api.py --apply
"""
import os
import sys
import argparse
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


def _make_resilient_session():
    """A requests.Session that auto-retries on transient connection blips
    (RemoteDisconnected, 5xx). Football-data.org and ESPN both occasionally
    return half-open connections — without retries the whole cron tick
    crashes and we lose ~3 min of liveness."""
    s = requests.Session()
    retry = Retry(
        total=4, backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


HTTP = _make_resilient_session()


COMPETITION   = "2000"   # FIFA World Cup on football-data.org
API_URL       = f"https://api.football-data.org/v4/competitions/{COMPETITION}/matches"
LIVE_STATUSES = {"IN_PLAY", "PAUSED", "LIVE"}

# ESPN's public WC scoreboard. No auth. Gives us:
#   - status.displayClock ("21'", "45+2'", "HT")
#   - status.type.state ("in", "pre", "post")
#   - score per competitor (mid-match live)
#   - venue.fullName / venue.address.city
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

# Team-name aliases for ESPN → football-data normalisation. ESPN uses
# slightly different long-form names for some teams; map to ours.
ESPN_TEAM_ALIASES = {
    "USA": "United States",
    "Korea Republic": "South Korea",
    "IR Iran": "Iran",
    "Republic of Ireland": "Ireland",
    # add more as discovered in the wild
}


def _normalise_team(name):
    if not name:
        return ""
    name = name.strip()
    return ESPN_TEAM_ALIASES.get(name, name)


def _parse_minute(display_clock, period):
    """Turn ESPN's displayClock string into a minute integer. Examples:
        "21'"     → 21
        "45+2'"   → 45
        "0'"      → 0
        "HT"      → 45  (halftime)
        "FT"      → 90
    Returns None on parse failure."""
    if not display_clock:
        return None
    s = str(display_clock).strip()
    if s in ("HT", "Halftime"):
        return 45
    if s in ("FT", "Full Time"):
        return 90
    import re as _re
    m = _re.match(r"(\d+)", s)
    if m:
        try:
            base = int(m.group(1))
            return min(120, base)
        except ValueError:
            pass
    return None


def fetch_espn_live(fixtures_by_team_pair):
    """Return list of {fixture_id, home, away, status, minute, city, updated_at}
    for every ESPN event in state=in that matches one of our fixtures by
    (home_team, away_team)."""
    out = []
    try:
        r = HTTP.get(ESPN_URL, timeout=15)
        if r.status_code != 200:
            print(f"  ESPN HTTP {r.status_code} — skipping ESPN pass")
            return out
        data = r.json()
    except Exception as e:
        print(f"  ESPN fetch failed: {e}")
        return out

    for evt in data.get("events", []):
        status_obj = evt.get("status") or {}
        state = (status_obj.get("type") or {}).get("state", "")
        if state not in ("in", "live"):
            continue
        comps = (evt.get("competitions") or [{}])[0].get("competitors", [])
        if len(comps) != 2:
            continue
        home_c = next((c for c in comps if c.get("homeAway") == "home"), None)
        away_c = next((c for c in comps if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue
        home_team = _normalise_team(home_c.get("team", {}).get("displayName"))
        away_team = _normalise_team(away_c.get("team", {}).get("displayName"))
        fixture_id = fixtures_by_team_pair.get((home_team, away_team))
        if not fixture_id:
            continue
        # Score (None if not started — but state=in shouldn't see that)
        try:
            h_score = int(home_c.get("score") or 0)
            a_score = int(away_c.get("score") or 0)
        except (ValueError, TypeError):
            continue
        minute = _parse_minute(status_obj.get("displayClock"),
                                status_obj.get("period"))
        venue = (evt.get("competitions") or [{}])[0].get("venue") or {}
        city = (venue.get("address") or {}).get("city")
        out.append({
            "fixture_id": fixture_id,
            "home": h_score, "away": a_score,
            "status": "IN_PLAY",
            "minute": minute,
            "city": city,
            "updated_at": evt.get("date") or "",
            "label": f"{home_team} {h_score}-{a_score} {away_team}",
        })
    return out


def derive_result(home, away, h_pens=None, a_pens=None):
    """Penalty shootout overrides the regulation result if present."""
    if h_pens is not None and a_pens is not None:
        return "H" if h_pens > a_pens else "A"
    if home > away:  return "H"
    if away > home:  return "A"
    return "D"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="actually write scores + score picks (default: preview)")
    args = p.parse_args(argv)
    dry_run = not args.apply

    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set")
        if argv is None: sys.exit(1)
        return
    if not os.environ.get("DATABASE_URL", "").strip():
        # SQLite path is fine for local dev runs; only block on prod.
        if argv is None and not os.environ.get("ALLOW_SQLITE"):
            print("ERROR: DATABASE_URL not set"); sys.exit(1)

    if dry_run:
        print("=== DRY-RUN (no writes). Add --apply to commit. ===\n")

    # Wrap in try/except too — Retry handles 5xx + connect errors but a
    # full crash from football-data should still log + exit gracefully so
    # Railway shows a non-zero status instead of an opaque restart.
    try:
        r = HTTP.get(API_URL, headers={"X-Auth-Token": api_key}, timeout=20)
    except requests.exceptions.RequestException as e:
        print(f"football-data fetch failed (transient): {e}")
        sys.exit(0)   # exit 0 so Railway doesn't loop-restart
    if r.status_code != 200:
        print(f"API error {r.status_code}: {r.text[:300]}"); sys.exit(1)
    matches = r.json().get("matches", [])

    by_status = {}
    for m in matches:
        s = m.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1
    print(f"API returned {len(matches)} matches  ({by_status})")

    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, home_team, away_team, result, "
        "       home_score, away_score, "
        "       home_penalties, away_penalties, "
        "       live_home_score, live_away_score, live_status "
        "FROM fixtures"
    ).fetchall()
    fixtures_by_id = {row["id"]: dict(row) for row in rows}
    conn.close()

    finalise = []      # FINISHED + not yet scored
    live_updates = []  # IN_PLAY / PAUSED — write live_*
    live_clears  = []  # FINISHED + has stale live_* rows we should wipe
    skipped_not_in_db = 0
    skipped_no_score  = 0
    corrections = []   # FINISHED but our existing scores disagree with the API

    for m in matches:
        fid    = str(m["id"])
        status = m.get("status")
        if fid not in fixtures_by_id:
            skipped_not_in_db += 1
            continue
        existing = fixtures_by_id[fid]
        score_obj = m.get("score") or {}
        ft = score_obj.get("fullTime") or {}
        home, away = ft.get("home"), ft.get("away")
        duration = score_obj.get("duration") or ""
        h_pens = a_pens = None
        # Penalty-shootout games need special handling. football-data's
        # `fullTime` field conflates the shootout into the final score
        # (a 1-1 game decided 5-4 on pens appears as fullTime 4-5).
        # Use `regularTime` for the end-of-90 score, add `extraTime` for
        # ET, and derive the shootout score by subtraction (the API's
        # `penalties` field is sometimes inconsistent -- e.g. returns
        # 4-4 for a 5-4 shootout).
        if duration == "PENALTY_SHOOTOUT":
            rt = score_obj.get("regularTime") or {}
            et = score_obj.get("extraTime") or {}
            reg_h, reg_a = rt.get("home"), rt.get("away")
            if reg_h is not None:
                reg_h += (et.get("home") or 0)
            if reg_a is not None:
                reg_a += (et.get("away") or 0)
            if reg_h is not None and reg_a is not None:
                # Pull shootout out of the conflated fullTime.
                if ft.get("home") is not None and ft.get("away") is not None:
                    h_pens = ft["home"] - reg_h
                    a_pens = ft["away"] - reg_a
                home, away = reg_h, reg_a
            else:
                # regularTime missing -- fall back to penalties block
                # for the shootout, even if its arithmetic is suspect.
                pens = score_obj.get("penalties") or {}
                h_pens, a_pens = pens.get("home"), pens.get("away")
                if h_pens == a_pens and h_pens is not None:
                    # Tied pen count but somebody won -- use winner
                    # field to break the tie so result code is correct.
                    winner = score_obj.get("winner") or ""
                    if winner == "HOME_TEAM":
                        h_pens, a_pens = h_pens + 1, a_pens
                    elif winner == "AWAY_TEAM":
                        h_pens, a_pens = h_pens, a_pens + 1

        if status == "FINISHED":
            if existing.get("result"):
                # Already settled in our DB. Clear any stale live_* snapshot.
                if existing.get("live_status") is not None:
                    live_clears.append(fid)
                # Self-heal: if football-data now shows a different score
                # than what we settled with (e.g. mid-game snapshot got
                # frozen, or an admin corrected the API record), schedule
                # a re-score so process_fixture_result can reverse old
                # payouts and re-apply with the correct line.
                # Trigger a correction only when the score actually
                # differs. Includes pens so a row that picked up a
                # conflated fullTime score gets fixed once we capture
                # the proper regulation + pens split.
                #
                # Safety guard for unreliable shootout data: when
                # duration=PENALTY_SHOOTOUT but score.winner is null,
                # the API itself can't tell who won (e.g. NL-MAR R32:
                # fullTime said 4-3 NL while ESPN had MAR winning the
                # shootout 3-2). In that case the existing DB row may
                # have been manually corrected by an admin -- don't
                # auto-flip it back to the wrong winner. The fixture is
                # already settled, so leaving it as-is is the safe
                # default. Live scores and IN_PLAY snapshots are
                # unaffected; only the FINISHED correction branch skips.
                api_winner_known = bool(score_obj.get("winner"))
                if (duration == "PENALTY_SHOOTOUT"
                        and not api_winner_known
                        and existing.get("result")):
                    continue
                if home is not None and away is not None and (
                    existing.get("home_score") != home
                    or existing.get("away_score") != away
                    or (h_pens is not None and existing.get("home_penalties") != h_pens)
                    or (a_pens is not None and existing.get("away_penalties") != a_pens)
                ):
                    corrections.append({
                        "id": fid, "home": home, "away": away,
                        "h_pens": h_pens, "a_pens": a_pens,
                        "old_home": existing.get("home_score"),
                        "old_away": existing.get("away_score"),
                        "result": derive_result(home, away, h_pens, a_pens),
                        "label": (f'{m["homeTeam"]["name"]} {home}-{away}'
                                  + (f' ({h_pens}-{a_pens} pens)' if h_pens is not None else '')
                                  + f' {m["awayTeam"]["name"]}'),
                    })
                continue
            if home is None or away is None:
                skipped_no_score += 1
                continue
            finalise.append({
                "id": fid, "home": home, "away": away,
                "h_pens": h_pens, "a_pens": a_pens,
                "result": derive_result(home, away, h_pens, a_pens),
                "label": (f'{m["homeTeam"]["name"]} {home}-{away}'
                          + (f' ({h_pens}-{a_pens} pens)' if h_pens is not None else '')
                          + f' {m["awayTeam"]["name"]}'),
            })
        elif status in LIVE_STATUSES:
            if home is None or away is None:
                continue
            live_updates.append({
                "id":         fid,
                "home":       home,
                "away":       away,
                "status":     status,
                "updated_at": m.get("lastUpdated") or "",
                "label":      f'{m["homeTeam"]["name"]} {home}-{away} {m["awayTeam"]["name"]}',
            })

    print(f"finalise (FINISHED, not yet scored): {len(finalise)}")
    print(f"corrections (score now differs):     {len(corrections)}")
    print(f"live updates (IN_PLAY / PAUSED):     {len(live_updates)}")
    print(f"stale live rows to clear:            {len(live_clears)}")
    print(f"not in our fixtures table:           {skipped_not_in_db}")
    print(f"FINISHED but no fullTime score:      {skipped_no_score}\n")

    for w in finalise:
        print(f"  FINALISE  {w['label']}   →  result={w['result']}")
    for w in corrections:
        print(f"  CORRECT   {w['label']}   (was {w['old_home']}-{w['old_away']}, "
              f"now {w['home']}-{w['away']}, result={w['result']})")
    for w in live_updates:
        print(f"  LIVE      {w['label']}    [{w['status']}]")
    for fid in live_clears:
        print(f"  CLEAR     {fid}   (FINISHED, stale live_*)")

    if dry_run:
        print("\n(dry-run — no writes. Re-run with --apply to commit.)")
        return

    for w in finalise:
        db.update_fixture_result(w["id"], w["home"], w["away"],
                                  w.get("h_pens"), w.get("a_pens"))
        # Wipe any earlier IN_PLAY snapshot before settling.
        db.clear_live_score(w["id"])
        n_picks = db.process_fixture_result(w["id"])
        print(f"  ✓ scored {w['label']}  ({n_picks} picks processed)")
    for w in corrections:
        # Overwrite the wrong scores. process_fixture_result is idempotent —
        # it reverses prior payouts and re-applies based on the new result,
        # so every affected user's balance reconciles cleanly.
        db.update_fixture_result(w["id"], w["home"], w["away"],
                                  w.get("h_pens"), w.get("a_pens"))
        db.clear_live_score(w["id"])
        n_picks = db.process_fixture_result(w["id"])
        print(f"  ✓ corrected {w['label']}  ({n_picks} picks re-processed)")
    for w in live_updates:
        db.update_live_score(w["id"], w["home"], w["away"], w["status"], w["updated_at"])
    for fid in live_clears:
        db.clear_live_score(fid)

    # ── ESPN pass: enrich live snapshots with minute + venue city ──────
    fixtures_by_team_pair = {
        (r["home_team"], r["away_team"]): r["id"]
        for r in rows
        if r["home_team"] and r["away_team"]
    }
    espn_live = fetch_espn_live(fixtures_by_team_pair)
    for w in espn_live:
        db.update_live_score(
            w["fixture_id"], w["home"], w["away"], w["status"],
            w["updated_at"], minute=w["minute"], city=w["city"],
        )
        print(f"  ESPN     {w['label']}    minute={w['minute']}  city={w['city']!r}")

    print(f"\ndone — {len(finalise)} finalised, {len(live_updates)} live (FD), "
          f"{len(espn_live)} live (ESPN), {len(live_clears)} cleared")


if __name__ == "__main__":
    main()
