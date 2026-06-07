"""
sim/export_prod.py — pull a complete, readable snapshot of a Postgres pool DB
into timestamped CSV + JSON files on your laptop.

What it exports (in sim/exports/<timestamp>/):
  - picks.csv         — one row per pick, with human-readable team/user fields
  - balances.csv      — current balance per user per pool
  - transactions.csv  — every economy event (bet, payout, spy, adjustment)
  - fixtures.csv      — fixture list with results, KO odds, ET kick-off
  - users.csv         — display name, email, admin flag
  - pools.csv         — pool config (entry fee, payment instructions)
  - scoring_config.csv — every scoring/economy config row (history preserved)
  - snapshot.json     — entire DB state as a single JSON document
  - SUMMARY.txt       — human-readable counts + integrity checks

Usage:
  DATABASE_URL='postgresql://...' python3 sim/export_prod.py

Run as often as you want — every export is a fresh folder. Disk usage
is minimal (~1 MB per snapshot for a 100-player pool).

If the site breaks mid-tournament you can:
  - Audit picks.csv manually (Excel friendly)
  - Reconstruct any user's balance: starting + sum(transactions.amount)
  - Settle the pool from the export alone
"""
import os
import sys
import csv
import json
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def to_et(iso_str):
    """Convert a UTC ISO string to America/New_York display format."""
    if not iso_str:
        return ""
    s = str(iso_str)
    try:
        # psycopg2 may give us a datetime obj already
        if isinstance(iso_str, datetime):
            dt = iso_str
        else:
            dt = datetime.fromisoformat(s[:19].rstrip("Z"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except (ValueError, TypeError):
        return s


def write_csv(path, rows, header):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def main():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: set $DATABASE_URL first (Postgres connection string from Railway)")
        sys.exit(1)

    stamp = datetime.now(ET).strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(os.path.dirname(__file__), "exports", stamp)
    os.makedirs(out, exist_ok=True)
    print(f"\nExporting to: {out}")

    conn = psycopg2.connect(url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Users ──────────────────────────────────────────────────────────────
    cur.execute("""
        SELECT id, display_name, email, is_admin, created_at
        FROM users ORDER BY created_at
    """)
    users = [dict(r) for r in cur.fetchall()]
    user_by_id = {u["id"]: u for u in users}
    write_csv(os.path.join(out, "users.csv"),
              [(u["id"], u["display_name"], u["email"], u["is_admin"], to_et(u["created_at"]))
               for u in users],
              ["id", "display_name", "email", "is_admin", "created_at"])
    print(f"  users.csv         {len(users)}")

    # ── Pools ──────────────────────────────────────────────────────────────
    cur.execute("SELECT * FROM pools ORDER BY created_at")
    pools = [dict(r) for r in cur.fetchall()]
    pool_by_id = {p["id"]: p for p in pools}
    write_csv(os.path.join(out, "pools.csv"),
              [(p["id"], p["name"], p.get("description") or "", p["is_public"],
                p.get("entry_fee") or "", p.get("payment_instructions") or "",
                to_et(p["created_at"])) for p in pools],
              ["id", "name", "description", "is_public", "entry_fee",
               "payment_instructions", "created_at"])
    print(f"  pools.csv         {len(pools)}")

    # ── Fixtures ───────────────────────────────────────────────────────────
    cur.execute("SELECT * FROM fixtures ORDER BY kick_off, id")
    fixtures = [dict(r) for r in cur.fetchall()]
    fixture_by_id = {f["id"]: f for f in fixtures}
    write_csv(os.path.join(out, "fixtures.csv"),
              [(f["id"], f["stage"], f["home_team"], f["away_team"],
                to_et(f["kick_off"]),
                f.get("home_score"), f.get("away_score"), f.get("result") or "",
                f.get("home_odds"), f.get("away_odds")) for f in fixtures],
              ["id", "stage", "home_team", "away_team", "kick_off_ET",
               "home_score", "away_score", "result", "home_odds", "away_odds"])
    print(f"  fixtures.csv      {len(fixtures)}")

    # ── Picks (the most important table — joined for readability) ─────────
    cur.execute("""
        SELECT p.id, p.user_id, u.display_name, u.email,
               p.pool_id, po.name AS pool_name,
               p.fixture_id, f.stage, f.home_team, f.away_team,
               f.kick_off, f.result AS fixture_result,
               p.predicted_result, p.bet_amount, p.submitted_at
        FROM picks p
        JOIN users u    ON u.id  = p.user_id
        JOIN pools po   ON po.id = p.pool_id
        JOIN fixtures f ON f.id  = p.fixture_id
        ORDER BY po.name, f.kick_off, u.display_name
    """)
    picks = [dict(r) for r in cur.fetchall()]
    write_csv(os.path.join(out, "picks.csv"),
              [(p["pool_name"], p["display_name"], p["email"],
                p["stage"], f'{p["home_team"]} vs {p["away_team"]}',
                to_et(p["kick_off"]), p["fixture_result"] or "",
                p["predicted_result"], p["bet_amount"] or "",
                ("correct" if p["fixture_result"] == p["predicted_result"]
                 else ("wrong" if p["fixture_result"] else "pending")),
                to_et(p["submitted_at"]),
                p["id"], p["user_id"], p["pool_id"], p["fixture_id"])
               for p in picks],
              ["pool", "display_name", "email",
               "stage", "match",
               "kick_off_ET", "result",
               "predicted", "bet_amount",
               "outcome",
               "submitted_at",
               "pick_id", "user_id", "pool_id", "fixture_id"])
    print(f"  picks.csv         {len(picks)}")

    # ── Balances (current snapshot per user per pool) ─────────────────────
    cur.execute("""
        SELECT pm.user_id, u.display_name, u.email,
               pm.pool_id, po.name AS pool_name,
               pm.has_paid, pm.balance, pm.joined_at
        FROM pool_members pm
        JOIN users u  ON u.id  = pm.user_id
        JOIN pools po ON po.id = pm.pool_id
        ORDER BY po.name, pm.balance DESC NULLS LAST
    """)
    balances = [dict(r) for r in cur.fetchall()]
    write_csv(os.path.join(out, "balances.csv"),
              [(b["pool_name"], b["display_name"], b["email"], b["has_paid"],
                f'{(b["balance"] or 0):.2f}', to_et(b["joined_at"]),
                b["user_id"], b["pool_id"])
               for b in balances],
              ["pool", "display_name", "email", "has_paid",
               "balance", "joined_at", "user_id", "pool_id"])
    print(f"  balances.csv      {len(balances)}")

    # ── Transactions (full economy ledger, most recent first) ─────────────
    cur.execute("""
        SELECT t.id, t.user_id, u.display_name, u.email,
               t.pool_id, po.name AS pool_name,
               t.fixture_id, f.home_team, f.away_team,
               t.type, t.amount, t.description, t.created_at
        FROM transactions t
        JOIN users u       ON u.id  = t.user_id
        JOIN pools po      ON po.id = t.pool_id
        LEFT JOIN fixtures f ON f.id = t.fixture_id
        ORDER BY t.created_at DESC
    """)
    txs = [dict(r) for r in cur.fetchall()]
    write_csv(os.path.join(out, "transactions.csv"),
              [(to_et(t["created_at"]), t["pool_name"], t["display_name"], t["email"],
                t["type"], f'{t["amount"]:.2f}', t["description"] or "",
                (f'{t["home_team"]} vs {t["away_team"]}' if t["home_team"] else ""),
                t["id"], t["user_id"], t["pool_id"], t["fixture_id"] or "")
               for t in txs],
              ["created_at_ET", "pool", "display_name", "email",
               "type", "amount", "description", "match",
               "tx_id", "user_id", "pool_id", "fixture_id"])
    print(f"  transactions.csv  {len(txs)}")

    # ── Scoring config history ────────────────────────────────────────────
    cur.execute("SELECT * FROM scoring_config ORDER BY updated_at")
    scoring = [dict(r) for r in cur.fetchall()]
    if scoring:
        header = list(scoring[0].keys())
        write_csv(os.path.join(out, "scoring_config.csv"),
                  [[r.get(k) for k in header] for r in scoring], header)
    print(f"  scoring_config.csv {len(scoring)}")

    # ── Single snapshot JSON for full state ───────────────────────────────
    def _serialise(v):
        if isinstance(v, datetime):
            return v.isoformat()
        return v
    snapshot = {
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "exported_at_et":  datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "users":           users,
        "pools":           pools,
        "fixtures":        fixtures,
        "picks":           picks,
        "balances":        balances,
        "transactions":    txs,
        "scoring_config":  scoring,
    }
    with open(os.path.join(out, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, default=_serialise, indent=2)
    print(f"  snapshot.json")

    # ── Integrity checks (printed + saved) ────────────────────────────────
    lines = []
    lines.append(f"WC26 Pool — export summary  ({stamp} ET)\n")
    lines.append(f"  users:         {len(users)}")
    lines.append(f"  pools:         {len(pools)}")
    lines.append(f"  fixtures:      {len(fixtures)}  "
                 f"({sum(1 for f in fixtures if f.get('result'))} with results)")
    lines.append(f"  picks:         {len(picks)}")
    lines.append(f"  balances:      {len(balances)}")
    lines.append(f"  transactions:  {len(txs)}")
    lines.append("")

    # Per-pool integrity: sum(balances) should equal sum(starting + transactions)
    for p in pools:
        pid = p["id"]
        pool_members = [b for b in balances if b["pool_id"] == pid]
        pool_txs     = [t for t in txs      if t["pool_id"] == pid]
        n_members    = len(pool_members)
        total_bal    = sum(float(b["balance"] or 0) for b in pool_members)
        tx_sum       = sum(float(t["amount"])      for t in pool_txs)
        # Starting balance per user — read from latest scoring config for this pool
        latest = None
        for s in scoring:
            if s.get("pool_id") in (None, pid):
                latest = s  # last one wins because we sorted by updated_at
        start_per_user = float(latest.get("starting_balance") or 100) if latest else 100.0
        expected = n_members * start_per_user + tx_sum
        diff     = total_bal - expected
        status   = "OK" if abs(diff) < 0.01 else "MISMATCH"
        lines.append(f"  [{status}] {p['name']!r}")
        lines.append(f"      members:   {n_members}")
        lines.append(f"      balances:  ${total_bal:,.2f}")
        lines.append(f"      starting:  ${n_members * start_per_user:,.2f}  "
                     f"(per user ${start_per_user})")
        lines.append(f"      tx_sum:    ${tx_sum:,.2f}")
        lines.append(f"      expected:  ${expected:,.2f}")
        lines.append(f"      diff:      ${diff:+,.4f}")
        lines.append("")

    summary = "\n".join(lines)
    print("\n" + summary)
    with open(os.path.join(out, "SUMMARY.txt"), "w", encoding="utf-8") as f:
        f.write(summary)

    cur.close()
    conn.close()
    print(f"\nDone. Snapshot at: {out}\n")


if __name__ == "__main__":
    main()
