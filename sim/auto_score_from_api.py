"""
sim/auto_score_from_api.py — pull final scores from football-data.org and
write them back to fixtures + score every affected pool's picks.

For each FINISHED match returned by the API:
  1. Look up the fixture by id (the app stores the football-data match id
     as fixtures.id, so this is a direct PK lookup).
  2. If the fixture is already scored (result IS NOT NULL), skip.
  3. Otherwise UPDATE home_score / away_score / result.
  4. Call calculate_scores_for_fixture(fid) to award points to all picks.
     That helper is idempotent via the score_log UNIQUE(pick_id) constraint.

Default is DRY-RUN: prints what would be written but touches nothing.
Add --apply to actually write.

Usage:
    DATABASE_URL='...' FOOTBALL_DATA_API_KEY='...' \\
        python3 sim/auto_score_from_api.py
    # Then if the preview is right:
    DATABASE_URL='...' FOOTBALL_DATA_API_KEY='...' \\
        python3 sim/auto_score_from_api.py --apply
"""
import os
import sys
import argparse
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


COMPETITION = "2000"   # FIFA World Cup on football-data.org
API_URL     = f"https://api.football-data.org/v4/competitions/{COMPETITION}/matches"


def derive_result(home, away):
    if home > away:  return "H"
    if away > home:  return "A"
    return "D"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="actually write scores + score picks (default: preview)")
    args = p.parse_args()
    dry_run = not args.apply

    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set"); sys.exit(1)
    if not os.environ.get("DATABASE_URL", "").strip():
        print("ERROR: DATABASE_URL not set"); sys.exit(1)

    print("=== DRY-RUN (no writes). Add --apply to commit. ===\n" if dry_run else "")

    r = requests.get(API_URL, headers={"X-Auth-Token": api_key}, timeout=20)
    if r.status_code != 200:
        print(f"API error {r.status_code}: {r.text[:300]}"); sys.exit(1)
    matches = r.json().get("matches", [])
    finished = [m for m in matches if m.get("status") == "FINISHED"]
    print(f"API returned {len(matches)} matches, {len(finished)} FINISHED")

    if not finished:
        print("no finished matches — nothing to do"); return

    conn = db.get_db()
    rows = conn.execute("SELECT id, home_team, away_team, result FROM fixtures").fetchall()
    fixtures_by_id = {r["id"]: dict(r) for r in rows}
    conn.close()

    to_write = []
    skipped_already_scored = 0
    skipped_not_in_db      = 0
    skipped_no_score       = 0
    for m in finished:
        fid = str(m["id"])
        if fid not in fixtures_by_id:
            skipped_not_in_db += 1
            continue
        if fixtures_by_id[fid].get("result"):
            skipped_already_scored += 1
            continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        home, away = ft.get("home"), ft.get("away")
        if home is None or away is None:
            skipped_no_score += 1
            continue
        result = derive_result(home, away)
        to_write.append({
            "id": fid, "home": home, "away": away, "result": result,
            "label": f'{m["homeTeam"]["name"]} {home}-{away} {m["awayTeam"]["name"]}',
        })

    print(f"already scored in DB: {skipped_already_scored}")
    print(f"not in our fixtures table: {skipped_not_in_db}")
    print(f"FINISHED but no fullTime score: {skipped_no_score}")
    print(f"to write: {len(to_write)}\n")

    for w in to_write:
        print(f"  {w['label']}   →  result={w['result']}")
    if not to_write:
        return
    if dry_run:
        print("\n(dry-run — no writes. Re-run with --apply to commit.)")
        return

    # Apply: update each fixture + score its picks. Each helper opens its own
    # connection, but the total work here is tiny (a handful of fixtures per
    # run), so per-call connection cost is fine.
    for w in to_write:
        db.update_fixture_result(w["id"], w["home"], w["away"])
        db.calculate_scores_for_fixture(w["id"])
        print(f"  ✓ scored {w['label']}")


if __name__ == "__main__":
    main()
