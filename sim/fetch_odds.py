"""
sim/fetch_odds.py — pull WC26 odds from The Odds API and lock them at
kick_off − LOCK_HOURS.

For each match returned by The Odds API:
  - Match it to a DB fixture by team names + kick_off (within 4 h
    tolerance to handle minor timezone slippage between providers).
  - If the row is already `odds_locked_at`, skip writes.
  - If kick_off is more than LOCK_HOURS away, update home_odds / away_odds
    with Pinnacle's decimal line.
  - If kick_off is inside the LOCK_HOURS window, freeze the current odds
    (stamp `odds_locked_at`) and stop updating future runs.

Idempotent — re-runs just refresh prices on unlocked rows or no-op on
locked ones. Default is APPLY (matches auto_score_from_api.py); pass
`--dry-run` to preview without writing.

Required env: THE_ODDS_API_KEY · DATABASE_URL (or local SQLite).

Usage:
    THE_ODDS_API_KEY='...' DATABASE_URL='...' python3 sim/fetch_odds.py
    THE_ODDS_API_KEY='...' DATABASE_URL='...' python3 sim/fetch_odds.py --dry-run
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


LOCK_HOURS = 12
SPORT = "soccer_fifa_world_cup"
ENDPOINT = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
TIMEOUT = 20

# Pinnacle is the "sharp money" baseline — see SPEC_KO.md. The API ID for
# Pinnacle in The Odds API's bookmaker list is the literal lowercase key.
BOOKMAKER = "pinnacle"

# Tolerance for matching API kick_off to our DB kick_off. Either provider
# can be a few hours off due to TZ confusion or schedule updates; if the
# match is within MATCH_KO_HOURS of one another we treat it as the same
# fixture so long as the team names line up.
MATCH_KO_HOURS = 4


def _parse_iso(ts):
    if not ts:
        return None
    s = ts.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_events(api_key):
    params = {
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    "h2h",
        "bookmakers": BOOKMAKER,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    r = requests.get(ENDPOINT, params=params, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"The Odds API error {r.status_code}: {r.text[:200]}")
    return r.json()


def extract_pinnacle_h2h(event):
    """Return (home_odds, away_odds) decimal for this match's Pinnacle h2h
    line, or (None, None) if not posted."""
    home = (event.get("home_team") or "").strip()
    away = (event.get("away_team") or "").strip()
    for bm in event.get("bookmakers", []):
        if bm.get("key") != BOOKMAKER:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            outcomes = {o.get("name"): o.get("price") for o in mkt.get("outcomes", [])}
            return outcomes.get(home), outcomes.get(away)
    return None, None


def ensure_odds_locked_at_column(conn):
    """Idempotently add fixtures.odds_locked_at. Lets this cron run on a
    prod DB whose code hasn't been migrated yet — important when the cron
    service is deployed before / after the next app push."""
    try:
        # Postgres
        conn.execute("ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS odds_locked_at TEXT")
        return
    except Exception:
        pass
    try:
        # SQLite — no IF NOT EXISTS for ADD COLUMN. Probe first.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fixtures)").fetchall()}
        if "odds_locked_at" not in cols:
            conn.execute("ALTER TABLE fixtures ADD COLUMN odds_locked_at TEXT")
    except Exception as e:
        print(f"warn: could not ensure odds_locked_at column: {e}")


# The Odds API and football-data.org use slightly different country/team
# strings. Pinnacle's name is the key, football-data's name is the value.
# Each Pinnacle name is also tried verbatim before any alias lookup.
TEAM_ALIASES = {
    "USA":                    "United States",
    "Bosnia & Herzegovina":   "Bosnia-Herzegovina",
    "Cape Verde":             "Cape Verde Islands",
    "DR Congo":               "Congo DR",
    "Korea Republic":         "South Korea",
    "Korea DPR":              "North Korea",
    "Türkiye":                "Turkey",
}


def _name_variants(name):
    """Return the Pinnacle name followed by any football-data alias."""
    variants = [name]
    if name in TEAM_ALIASES:
        variants.append(TEAM_ALIASES[name])
    return variants


def find_db_fixture(conn, home, away, ko_dt):
    """Match an API event to a fixture. Tries each home/away name and its
    football-data alias; kick_off must be within MATCH_KO_HOURS of the API
    time.

    Returns (row_dict, debug) where debug is None on success or a short
    reason string on failure ('no_team_match' / 'ko_drift'). Useful for
    --verbose diagnostics so we can see exactly why a no_match happened."""
    rows = []
    for h in _name_variants(home):
        for a in _name_variants(away):
            rows = conn.execute(
                "SELECT id, kick_off, odds_locked_at, home_odds, away_odds, "
                "       home_team, away_team "
                "  FROM fixtures "
                " WHERE LOWER(home_team) = LOWER(?) AND LOWER(away_team) = LOWER(?)",
                (h, a),
            ).fetchall()
            if rows:
                break
        if rows:
            break
    if not rows:
        return None, "no_team_match"
    if not ko_dt or len(rows) == 1:
        return dict(rows[0]), None
    # Multiple matches with the same teams — pick the one closest in time.
    best = None
    best_diff = None
    for r in rows:
        fix_ko = _parse_iso(r["kick_off"])
        if not fix_ko:
            continue
        diff = abs((fix_ko - ko_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = r, diff
    if best and best_diff and best_diff <= MATCH_KO_HOURS * 3600:
        return dict(best), None
    return None, "ko_drift"


def candidate_db_fixtures(conn, home, away):
    """For --verbose no-match diagnostics: return any DB rows whose home
    OR away team name contains either of the API team names (case-
    insensitive). Helps spot near-misses caused by name normalisation
    differences (e.g. 'USA' vs 'United States')."""
    rows = conn.execute(
        "SELECT id, home_team, away_team, kick_off "
        "  FROM fixtures "
        " WHERE LOWER(home_team) LIKE LOWER(?) OR LOWER(home_team) LIKE LOWER(?) "
        "    OR LOWER(away_team) LIKE LOWER(?) OR LOWER(away_team) LIKE LOWER(?) "
        " ORDER BY kick_off",
        (f"%{home}%", f"%{away}%", f"%{home}%", f"%{away}%"),
    ).fetchall()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview changes without committing.")
    ap.add_argument("--verbose", action="store_true",
                    help="Print every API event and (for no-match cases) "
                         "the nearest DB candidates so name/time drift "
                         "between providers can be diagnosed.")
    args = ap.parse_args()

    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        print("THE_ODDS_API_KEY not set — exiting.")
        return 0

    try:
        events = fetch_events(api_key)
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1
    print(f"Fetched {len(events)} matches from The Odds API ({BOOKMAKER}).")

    now = datetime.now(timezone.utc)
    conn = db.get_db()
    ensure_odds_locked_at_column(conn)

    updated = locked = no_change = skipped_locked = skipped_no_match = skipped_no_price = 0
    for ev in events:
        home = (ev.get("home_team") or "").strip()
        away = (ev.get("away_team") or "").strip()
        ko_dt = _parse_iso(ev.get("commence_time"))
        if not home or not away or not ko_dt:
            if args.verbose:
                print(f"  NO_MATCH  malformed event: home={home!r} away={away!r} ko={ev.get('commence_time')!r}")
            skipped_no_match += 1
            continue

        ho, ao = extract_pinnacle_h2h(ev)
        if ho is None or ao is None:
            if args.verbose:
                print(f"  NO_PRICE  {home} vs {away}  (Pinnacle hasn't posted yet)")
            skipped_no_price += 1
            continue

        row, why = find_db_fixture(conn, home, away, ko_dt)
        if not row:
            if args.verbose:
                cands = candidate_db_fixtures(conn, home, away)[:5]
                cand_lines = [
                    f"      {c['kick_off'] or '<no-ko>':<22}  {c['home_team']} vs {c['away_team']}"
                    for c in cands
                ]
                print(f"  NO_MATCH  {ko_dt.isoformat()}  {home} vs {away}  "
                      f"reason={why}")
                if cands:
                    print("    nearest DB candidates (any name overlap):")
                    print("\n".join(cand_lines))
                else:
                    print("    nothing in fixtures table contains either team name")
            skipped_no_match += 1
            continue

        if row["odds_locked_at"]:
            skipped_locked += 1
            continue

        fix_ko = _parse_iso(row["kick_off"]) or ko_dt
        diff_s = (fix_ko - now).total_seconds()
        if diff_s < 0:
            continue   # already kicked off

        same_price = (row.get("home_odds") == ho and row.get("away_odds") == ao)
        within_lock = diff_s <= LOCK_HOURS * 3600

        if within_lock:
            # Freeze the latest line: write odds + stamp odds_locked_at.
            if not args.dry_run:
                conn.execute(
                    "UPDATE fixtures "
                    "   SET home_odds=?, away_odds=?, odds_locked_at=? "
                    " WHERE id=?",
                    (ho, ao, now.isoformat(), row["id"]),
                )
            print(f"  LOCK   {home} vs {away}  {ho}/{ao}  (kickoff in {diff_s/3600:.1f}h)")
            locked += 1
        elif same_price:
            no_change += 1
        else:
            if not args.dry_run:
                conn.execute(
                    "UPDATE fixtures SET home_odds=?, away_odds=? WHERE id=?",
                    (ho, ao, row["id"]),
                )
            print(f"  UPDATE {home} vs {away}  {ho}/{ao}  (kickoff in {diff_s/3600:.1f}h)")
            updated += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"\n[{mode}] updated={updated} locked={locked} no_change={no_change} "
          f"skipped_locked={skipped_locked} skipped_no_match={skipped_no_match} "
          f"skipped_no_price={skipped_no_price}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
