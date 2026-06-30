"""
Restore R32 team names that were wiped by a bad football-data sync.

Step 1 (what this script does today): print group standings, the ranked
3rd-place teams, the 16 qualifiers, and the 16 R32 fixture slots we have
in the DB (each with its football-data ID and kick-off time). This is
DRY RUN ONLY -- it touches nothing.

Step 2 (next pass): bind each qualifier pair to its R32 fixture using
FIFA's WC26 bracket map, then UPDATE the 16 rows in one transaction.

Run:
    python3 -m sim.restore_r32
Against prod (point at the Railway DB):
    DATABASE_URL=... python3 -m sim.restore_r32
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

# Make sure we can import the project's database module when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_db
from fixtures import get_flag_code

ODDS_API_KEY = "12d7825cdf82f6371c4d9f8bfde37cdc"
ODDS_URL = (
    "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds"
    f"?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&bookmakers=pinnacle"
)

# Map Pinnacle's team naming to football-data's. Empty string means
# "Pinnacle uses the same name as football-data" -- only mismatches
# need listing.
PINNACLE_TO_FD = {
    "Bosnia & Herzegovina": "Bosnia-Herzegovina",
    "USA":                  "United States",
    "Cape Verde":           "Cape Verde Islands",
    "DR Congo":             "Congo DR",
}


def fetch_pinnacle_r32():
    """Return [{commence_time: dt, home: str, away: str}, ...]."""
    resp = requests.get(ODDS_URL, timeout=15)
    resp.raise_for_status()
    events = resp.json()
    out = []
    for e in events:
        h = PINNACLE_TO_FD.get(e["home_team"], e["home_team"])
        a = PINNACLE_TO_FD.get(e["away_team"], e["away_team"])
        ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        out.append({"commence_time": ct, "home": h, "away": a})
    return out


def parse_kickoff(s):
    """Robust ISO parse, returns UTC datetime or None."""
    if not s:
        return None
    try:
        # Handle both '2026-06-29T20:30:00Z' and '2026-06-29T20:30:00+00:00'
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def fetch_all_fixtures():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, home_team, away_team, kick_off, stage, "
        "home_score, away_score, result FROM fixtures"
    ).fetchall()
    conn.close()
    # Normalize to plain dicts (sqlite3.Row and RealDictCursor both work
    # with dict() / mapping access, but make it explicit).
    return [dict(r) for r in rows]


def group_standings(fixtures):
    """
    Return {group_letter: [ {team, played, w, d, l, gf, ga, gd, pts}, ... ]}
    sorted by FIFA tiebreaker: pts desc, gd desc, gf desc, name asc.
    """
    groups = defaultdict(list)  # group_letter -> list of fixture dicts
    for f in fixtures:
        stage = (f.get("stage") or "")
        if not stage.startswith("Group "):
            continue
        if f.get("result") is None:
            continue  # game not finished, can't count
        groups[stage[6:]].append(f)

    standings = {}
    for letter, fixes in groups.items():
        teams = defaultdict(lambda: {"played": 0, "w": 0, "d": 0, "l": 0,
                                     "gf": 0, "ga": 0})
        for f in fixes:
            h, a = f["home_team"], f["away_team"]
            hs, as_ = f["home_score"], f["away_score"]
            if hs is None or as_ is None:
                continue
            teams[h]["played"] += 1
            teams[a]["played"] += 1
            teams[h]["gf"] += hs
            teams[h]["ga"] += as_
            teams[a]["gf"] += as_
            teams[a]["ga"] += hs
            if hs > as_:
                teams[h]["w"] += 1; teams[a]["l"] += 1
            elif hs < as_:
                teams[a]["w"] += 1; teams[h]["l"] += 1
            else:
                teams[h]["d"] += 1; teams[a]["d"] += 1
        rows = []
        for t, s in teams.items():
            pts = s["w"] * 3 + s["d"]
            gd = s["gf"] - s["ga"]
            rows.append({"team": t, **s, "gd": gd, "pts": pts})
        rows.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["team"]))
        standings[letter] = rows
    return standings


def best_third_place(standings, take=8):
    """Return the top `take` third-place finishers across all groups."""
    thirds = []
    for letter, rows in standings.items():
        if len(rows) >= 3:
            thirds.append({"group": letter, **rows[2]})
    thirds.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["team"]))
    return thirds[:take]


def ko_fixtures_in_order(fixtures, stage_name="Round of 32"):
    """R32 fixtures sorted by football-data match ID (numeric)."""
    ko = [f for f in fixtures if (f.get("stage") or "") == stage_name]
    def sort_key(f):
        fid = f.get("id") or ""
        try:
            return (0, int(fid))
        except (TypeError, ValueError):
            return (1, fid)
    return sorted(ko, key=sort_key)


def match_pinnacle_to_slots(r32, pinnacle, qualifiers):
    """
    For each Pinnacle R32 event, find the DB fixture with matching
    kick_off (±1 hour) and validate both teams are in the qualifier set.

    Returns [(fixture_dict, pinnacle_event, ok_bool, reason_str), ...].
    Only events that match a slot are returned (Pinnacle entries that
    are actually group-stage MD3 games will be dropped).
    """
    tolerance_seconds = 3600
    results = []
    used_slots = set()
    for ev in pinnacle:
        best = None
        best_delta = None
        for f in r32:
            if f["id"] in used_slots:
                continue
            ko = parse_kickoff(f.get("kick_off"))
            if ko is None:
                continue
            delta = abs((ev["commence_time"] - ko).total_seconds())
            if delta <= tolerance_seconds and (best_delta is None or delta < best_delta):
                best = f
                best_delta = delta
        if best is None:
            continue  # Pinnacle event isn't an R32 slot (probably group MD3)
        used_slots.add(best["id"])
        h_ok = ev["home"] in qualifiers
        a_ok = ev["away"] in qualifiers
        ok = h_ok and a_ok
        reason = ""
        if not h_ok:
            reason += f" {ev['home']!r} not in qualifiers."
        if not a_ok:
            reason += f" {ev['away']!r} not in qualifiers."
        results.append((best, ev, ok, reason.strip()))
    return results


def apply_updates(matches):
    """Run the 16 (or fewer) UPDATE statements in one transaction."""
    conn = get_db()
    try:
        for fixture, ev, ok, _ in matches:
            if not ok:
                continue
            home = ev["home"]
            away = ev["away"]
            home_flag = get_flag_code(home)
            away_flag = get_flag_code(away)
            conn.execute(
                "UPDATE fixtures SET home_team=?, away_team=?, "
                "home_flag_code=?, away_flag_code=? WHERE id=?",
                (home, away, home_flag, away_flag, fixture["id"])
            )
        conn.commit()
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Run the UPDATE statements (default is dry-run).")
    parser.add_argument("--skip-pinnacle", action="store_true",
                        help="Skip Pinnacle pairing step (just show standings).")
    parser.add_argument("--skip-id", default="",
                        help="Comma-separated fixture IDs to exclude from the "
                             "apply step (still shown in dry-run).")
    args = parser.parse_args(argv)
    skip_ids = {s.strip() for s in args.skip_id.split(",") if s.strip()}

    fixtures = fetch_all_fixtures()
    print(f"Total fixtures in DB: {len(fixtures)}")
    print()

    standings = group_standings(fixtures)
    if not standings:
        print("No completed group-stage fixtures found. Cannot derive standings.")
        return

    print("=" * 64)
    print("GROUP STANDINGS")
    print("=" * 64)
    for letter in sorted(standings):
        rows = standings[letter]
        print(f"\nGroup {letter}:")
        print(f"  {'#':>2}  {'Team':<22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} "
              f"{'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>4}")
        for i, r in enumerate(rows, 1):
            print(f"  {i:>2}  {r['team']:<22} {r['played']:>2} {r['w']:>2} "
                  f"{r['d']:>2} {r['l']:>2} {r['gf']:>3} {r['ga']:>3} "
                  f"{r['gd']:>+4} {r['pts']:>4}")

    thirds = best_third_place(standings, take=8)
    print()
    print("=" * 64)
    print("RANKED 3rd-PLACE FINISHERS (top 8 qualify)")
    print("=" * 64)
    for i, r in enumerate(thirds, 1):
        print(f"  {i:>2}. Group {r['group']}  {r['team']:<22} "
              f"Pts={r['pts']}  GD={r['gd']:+d}  GF={r['gf']}")

    third_groups = {r["group"] for r in thirds}
    print()
    print(f"Groups whose 3rd place advanced: {sorted(third_groups)}")
    print(f"Groups whose 3rd place is out:   "
          f"{sorted(set(standings) - third_groups)}")

    print()
    print("=" * 64)
    print("R32 FIXTURE SLOTS (sorted by football-data match ID)")
    print("=" * 64)
    r32 = ko_fixtures_in_order(fixtures, "Round of 32")
    print(f"Found {len(r32)} R32 fixtures.\n")
    for i, f in enumerate(r32, 1):
        ko = f.get("kick_off") or "?"
        print(f"  Slot {i:>2}  id={f['id']:<10}  {ko}  "
              f"{f['home_team']} vs {f['away_team']}")

    if args.skip_pinnacle:
        print()
        print("Skipping Pinnacle pairing. Exiting.")
        return

    # Build the qualifier set: 12 group winners + 12 runners-up + top 8 thirds.
    qualifiers = set()
    for rows in standings.values():
        if len(rows) >= 1: qualifiers.add(rows[0]["team"])
        if len(rows) >= 2: qualifiers.add(rows[1]["team"])
    for r in thirds:
        qualifiers.add(r["team"])

    print()
    print("=" * 64)
    print(f"DERIVED QUALIFIERS ({len(qualifiers)} teams)")
    print("=" * 64)
    print("  " + ", ".join(sorted(qualifiers)))

    print()
    print("=" * 64)
    print("PINNACLE -> R32 SLOT BINDING")
    print("=" * 64)
    try:
        pinnacle = fetch_pinnacle_r32()
    except Exception as e:
        print(f"Failed to reach Odds API: {e}")
        return
    print(f"Pinnacle events fetched: {len(pinnacle)}")

    matched = match_pinnacle_to_slots(r32, pinnacle, qualifiers)
    print(f"Matched to R32 slots:    {len(matched)}")
    print()
    n_ok = sum(1 for _, _, ok, _ in matched if ok)
    n_warn = sum(1 for _, _, ok, _ in matched if not ok)
    for fixture, ev, ok, reason in matched:
        skipped = fixture["id"] in skip_ids
        if skipped:
            marker = "-- "
        else:
            marker = "OK " if ok else "!! "
        ko = fixture.get("kick_off")
        suffix = ""
        if reason:
            suffix = f"  -- {reason}"
        elif skipped:
            suffix = "  -- SKIPPED via --skip-id"
        print(f"  {marker}id={fixture['id']:<10}  {ko}  "
              f"{ev['home']} vs {ev['away']}{suffix}")
    print()
    apply_count = sum(1 for f, _, ok, _ in matched
                      if ok and f["id"] not in skip_ids)
    print(f"Would update {apply_count} fixture rows.")
    if n_warn:
        print(f"WARNING: {n_warn} pairings have teams missing from qualifiers; "
              "they will NOT be updated until you fix the mapping.")

    unmatched_slots = [f for f in r32 if f["id"] not in {m[0]["id"] for m in matched}]
    if unmatched_slots:
        print()
        print(f"R32 slots with no Pinnacle pairing yet ({len(unmatched_slots)} -- "
              "Pinnacle posts these once group MD3 finishes):")
        for f in unmatched_slots:
            print(f"  id={f['id']:<10}  {f.get('kick_off')}  "
                  f"{f['home_team']} vs {f['away_team']}")

    if args.apply:
        print()
        print("Applying updates...")
        to_apply = [m for m in matched if m[0]["id"] not in skip_ids]
        apply_updates(to_apply)
        print(f"Done. Updated {apply_count} R32 rows.")
    else:
        print()
        print("Dry run -- nothing was modified. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
