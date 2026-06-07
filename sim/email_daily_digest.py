"""
sim/email_daily_digest.py — once-a-day admin summary email.

Reports for the last 24h (configurable):
  - logins, registrations, picks placed
  - per-pool: member count, balance totals, accounting integrity check
  - upcoming fixtures in the next 24h (and how many have picks in)

Usage:
  DATABASE_URL='...' ADMIN_EMAILS='you@example.com,co@example.com' \
  SMTP_HOST='smtp.gmail.com' SMTP_USER='you@gmail.com' SMTP_PASS='app-pw' \
  python3 sim/email_daily_digest.py

Add --dry-run to print instead of send. Add --hours N to look back N hours
instead of the default 24.
"""
import os
import sys
import argparse
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from email_helper import send_email

ET = ZoneInfo("America/New_York")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hours",    type=int, default=24, help="look-back window")
    p.add_argument("--dry-run",  action="store_true",  help="print instead of send")
    args = p.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: $DATABASE_URL not set"); sys.exit(1)
    admin_emails = [e.strip() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
    if not admin_emails:
        print("ERROR: $ADMIN_EMAILS not set (comma-separated list)"); sys.exit(1)

    conn = psycopg2.connect(url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).replace(microsecond=0)

    # ── Totals over the window ─────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) AS n FROM picks WHERE submitted_at >= %s", (since,))
    n_picks = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM login_log WHERE created_at >= %s", (since,))
    n_logins = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(DISTINCT user_id) AS n FROM login_log WHERE created_at >= %s", (since,))
    n_unique_loggers = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM users WHERE created_at >= %s AND is_admin = 0", (since,))
    n_registrations = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 0")
    n_total_users = cur.fetchone()["n"]

    # ── Per-pool snapshot ──────────────────────────────────────────────────
    cur.execute("SELECT * FROM pools ORDER BY name")
    pools = cur.fetchall()
    pool_blocks = []
    for p in pools:
        pid = p["id"]
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(balance),0) AS bal "
                    "FROM pool_members WHERE pool_id=%s", (pid,))
        m = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM pool_members WHERE pool_id=%s AND has_paid=1", (pid,))
        paid = cur.fetchone()["n"]
        cur.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transactions WHERE pool_id=%s", (pid,))
        tx_sum = float(cur.fetchone()["s"])
        # Window-scoped pick count for this pool
        cur.execute("SELECT COUNT(*) AS n FROM picks WHERE pool_id=%s AND submitted_at >= %s",
                    (pid, since))
        new_picks = cur.fetchone()["n"]
        n_members = m["n"]; total_bal = float(m["bal"] or 0)
        starting  = n_members * 100.0  # assume default; better would be to read scoring_config
        expected  = starting + tx_sum
        diff      = total_bal - expected
        ok        = abs(diff) < 0.01
        pool_blocks.append({
            "name": p["name"], "members": n_members, "paid": paid,
            "balance_total": total_bal, "tx_sum": tx_sum,
            "expected": expected, "diff": diff, "accounting_ok": ok,
            "new_picks": new_picks,
        })

    # ── Upcoming fixtures (24h) ────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    horizon = now_utc + timedelta(hours=24)
    cur.execute("""
        SELECT f.id, f.stage, f.home_team, f.away_team, f.kick_off,
               (SELECT COUNT(*) FROM picks p WHERE p.fixture_id = f.id) AS pick_count
        FROM fixtures f
        WHERE f.kick_off >= %s AND f.kick_off <= %s AND f.result IS NULL
        ORDER BY f.kick_off
    """, (now_utc.isoformat(), horizon.isoformat()))
    upcoming = cur.fetchall()

    # ── Build the email body ───────────────────────────────────────────────
    now_et = datetime.now(ET).strftime("%a %b %d, %H:%M ET")
    lines = []
    lines.append(f"WC26 Pool — Daily Digest")
    lines.append(f"Generated {now_et}")
    lines.append("")
    lines.append(f"Window: last {args.hours}h")
    lines.append(f"  logins:         {n_logins}  ({n_unique_loggers} unique users)")
    lines.append(f"  registrations:  {n_registrations}")
    lines.append(f"  picks placed:   {n_picks}")
    lines.append(f"  total users:    {n_total_users}")
    lines.append("")

    for pb in pool_blocks:
        lines.append(f"── {pb['name']} ─────")
        lines.append(f"  members:        {pb['members']}  ({pb['paid']} paid)")
        lines.append(f"  picks (24h):    {pb['new_picks']}")
        lines.append(f"  balances total: ${pb['balance_total']:,.2f}")
        lines.append(f"  expected:       ${pb['expected']:,.2f}  "
                     f"(diff ${pb['diff']:+,.2f})")
        if not pb["accounting_ok"]:
            lines.append(f"  ⚠ ACCOUNTING DRIFT — investigate before next round")
        lines.append("")

    lines.append(f"── Kick off in next 24h ({len(upcoming)} fixtures) ─────")
    if upcoming:
        for f in upcoming:
            ko = f["kick_off"]
            ko_et = ko
            try:
                if isinstance(ko, str):
                    dt = datetime.fromisoformat(ko[:19].rstrip("Z"))
                else:
                    dt = ko
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ko_et = dt.astimezone(ET).strftime("%a %b %d %H:%M ET")
            except Exception:
                pass
            lines.append(f"  {ko_et:<22s} {f['stage']:<14s}  "
                         f"{f['home_team']} vs {f['away_team']}  "
                         f"({f['pick_count']} picks in)")
    else:
        lines.append("  (none)")

    body = "\n".join(lines)
    subject = f"[WC26] Daily digest — {n_picks} picks, {n_logins} logins, {len(upcoming)} fixtures upcoming"

    # ── Send ───────────────────────────────────────────────────────────────
    sent_to = send_email(admin_emails, subject, body, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"sent to: {sent_to}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
