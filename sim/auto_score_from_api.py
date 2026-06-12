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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


COMPETITION   = "2000"   # FIFA World Cup on football-data.org
API_URL       = f"https://api.football-data.org/v4/competitions/{COMPETITION}/matches"
LIVE_STATUSES = {"IN_PLAY", "PAUSED", "LIVE"}


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

    if dry_run:
        print("=== DRY-RUN (no writes). Add --apply to commit. ===\n")

    r = requests.get(API_URL, headers={"X-Auth-Token": api_key}, timeout=20)
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

    for m in matches:
        fid    = str(m["id"])
        status = m.get("status")
        if fid not in fixtures_by_id:
            skipped_not_in_db += 1
            continue
        existing = fixtures_by_id[fid]
        ft = (m.get("score") or {}).get("fullTime") or {}
        home, away = ft.get("home"), ft.get("away")

        if status == "FINISHED":
            if existing.get("result"):
                # Already settled in our DB — but if a stale live_* row is
                # still hanging around from an earlier IN_PLAY poll, clear it
                # so the pill stops rendering the live readout.
                if existing.get("live_status") is not None:
                    live_clears.append(fid)
                continue
            if home is None or away is None:
                skipped_no_score += 1
                continue
            finalise.append({
                "id": fid, "home": home, "away": away,
                "result": derive_result(home, away),
                "label": f'{m["homeTeam"]["name"]} {home}-{away} {m["awayTeam"]["name"]}',
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
    print(f"live updates (IN_PLAY / PAUSED):     {len(live_updates)}")
    print(f"stale live rows to clear:            {len(live_clears)}")
    print(f"not in our fixtures table:           {skipped_not_in_db}")
    print(f"FINISHED but no fullTime score:      {skipped_no_score}\n")

    for w in finalise:
        print(f"  FINALISE  {w['label']}   →  result={w['result']}")
    for w in live_updates:
        print(f"  LIVE      {w['label']}    [{w['status']}]")
    for fid in live_clears:
        print(f"  CLEAR     {fid}   (FINISHED, stale live_*)")

    if dry_run:
        print("\n(dry-run — no writes. Re-run with --apply to commit.)")
        return

    for w in finalise:
        db.update_fixture_result(w["id"], w["home"], w["away"])
        # Wipe any earlier IN_PLAY snapshot before settling.
        db.clear_live_score(w["id"])
        n_picks = db.process_fixture_result(w["id"])
        print(f"  ✓ scored {w['label']}  ({n_picks} picks processed)")
    for w in live_updates:
        db.update_live_score(w["id"], w["home"], w["away"], w["status"], w["updated_at"])
    for fid in live_clears:
        db.clear_live_score(fid)
    print(f"\ndone — {len(finalise)} finalised, {len(live_updates)} live, "
          f"{len(live_clears)} cleared")


if __name__ == "__main__":
    main()
