"""
sim/email_pick_reminders.py — nudge paid pool members who haven't picked
yet on fixtures that are about to lock.

For each fixture that kicks off in (--min-hours, --max-hours) from now and
isn't yet completed, find pool members who:
  - are members of any pool (since fixtures are global)
  - have paid (or pool has no entry_fee)
  - have NOT already submitted a pick for that fixture
  - have NOT already been reminded for that fixture (meta dedupe)

Send one email per user covering all their outstanding fixtures in window.
Mark each (user, fixture) reminder in the meta table so re-runs don't spam.

Usage:
  DATABASE_URL='...' SITE_URL='https://...' SMTP_HOST='...' [...] \\
  python3 sim/email_pick_reminders.py --min-hours 1 --max-hours 6

Add --dry-run to print emails instead of sending.

Cron suggestion: run hourly. --min-hours 1 --max-hours 6 means each fixture
gets at most one batch of reminders, sent roughly 5h before kickoff.
"""
import os
import sys
import argparse
import psycopg2
import psycopg2.extras
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from email_helper import send_email

ET = ZoneInfo("America/New_York")
META_PREFIX = "reminder_sent"  # key format: reminder_sent:<user_id>:<fixture_id>


def fmt_kickoff(ko):
    """Format an ISO/dt kick_off as 'Tue Jun 11 14:00 ET'."""
    try:
        dt = datetime.fromisoformat(str(ko)[:19].rstrip("Z")) if not isinstance(ko, datetime) else ko
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%a %b %d %H:%M ET")
    except Exception:
        return str(ko)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-hours", type=float, default=1,
                   help="lower bound on hours until kickoff (default 1)")
    p.add_argument("--max-hours", type=float, default=6,
                   help="upper bound on hours until kickoff (default 6)")
    p.add_argument("--dry-run",   action="store_true",
                   help="print emails instead of sending")
    args = p.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: $DATABASE_URL not set"); sys.exit(1)
    site_url = os.environ.get("SITE_URL", "https://poolboy2-app-production.up.railway.app").rstrip("/")

    conn = psycopg2.connect(url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    lo = now_utc + timedelta(hours=args.min_hours)
    hi = now_utc + timedelta(hours=args.max_hours)

    # Fixtures eligible for reminders: kicking off in window, not yet completed.
    # fixtures.kick_off is declared TEXT (so the schema works on sqlite too);
    # cast explicitly on Postgres so comparing against bound timestamps works.
    cur.execute("""
        SELECT id, stage, home_team, away_team, kick_off
        FROM fixtures
        WHERE kick_off::timestamptz >= %s
          AND kick_off::timestamptz <= %s
          AND result IS NULL
        ORDER BY kick_off::timestamptz
    """, (lo, hi))
    fixtures = cur.fetchall()
    if not fixtures:
        print(f"no fixtures in [{args.min_hours}h, {args.max_hours}h] from now — nothing to do")
        return
    print(f"{len(fixtures)} fixture(s) in window")

    # All pool members eligible to pick (paid, or pool has no entry fee)
    cur.execute("""
        SELECT pm.user_id, pm.pool_id, pm.has_paid,
               u.email, u.display_name,
               p.name AS pool_name, p.entry_fee
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        JOIN pools p ON p.id = pm.pool_id
    """)
    members = cur.fetchall()
    eligible_members = [m for m in members
                        if not m["entry_fee"] or m["has_paid"]]

    # Existing picks (so we skip users who already picked)
    cur.execute("SELECT user_id, pool_id, fixture_id FROM picks")
    existing = {(r["user_id"], r["pool_id"], r["fixture_id"]) for r in cur.fetchall()}

    # Existing reminder-sent meta keys (dedupe)
    cur.execute("SELECT key FROM meta WHERE key LIKE %s", (f"{META_PREFIX}:%",))
    reminded_keys = {r["key"] for r in cur.fetchall()}

    # Build per-user outstanding lists
    per_user_outstanding = defaultdict(list)  # email -> list[(member_row, fixture)]
    new_keys_to_record   = []                 # meta keys to insert after send

    for f in fixtures:
        fid = f["id"]
        for m in eligible_members:
            if (m["user_id"], m["pool_id"], fid) in existing:
                continue
            key = f"{META_PREFIX}:{m['user_id']}:{fid}"
            if key in reminded_keys:
                continue
            per_user_outstanding[m["email"]].append((m, f))
            new_keys_to_record.append(key)

    if not per_user_outstanding:
        print("no outstanding picks to remind on — done")
        return
    print(f"reminders to send: {len(per_user_outstanding)} user(s), "
          f"{len(new_keys_to_record)} (user, fixture) pair(s)")

    # ── Compose + send one email per user ──────────────────────────────────
    sent_any = False
    for email, items in sorted(per_user_outstanding.items()):
        # All items share email; user_display + pool may vary if user is in multiple pools
        display = items[0][0]["display_name"]
        n = len(items)
        # Pool deeplink — use the first pool the user is in (most common case: 1 pool)
        first_pool = items[0][0]["pool_id"]
        pool_url = f"{site_url}/pool/{first_pool}"

        lines = [
            f"Hey {display},",
            "",
            f"You have {n} outstanding pick{'s' if n != 1 else ''} "
            f"on fixture{'s' if n != 1 else ''} kicking off in the next "
            f"{args.max_hours:g} hours:",
            "",
        ]
        for m, f in items:
            lines.append(f"  • {fmt_kickoff(f['kick_off'])}  —  "
                         f"{f['home_team']} vs {f['away_team']}  "
                         f"({f['stage']}, pool: {m['pool_name']})")
        lines.append("")
        lines.append(f"Picks lock 15 minutes before kickoff. Hop in:")
        lines.append(pool_url)
        lines.append("")
        lines.append("— WC26 Pool")

        body = "\n".join(lines)
        subject = (f"WC26: {n} pick{'s' if n != 1 else ''} "
                   f"outstanding before kickoff")
        try:
            send_email(email, subject, body, dry_run=args.dry_run)
            sent_any = True
            print(f"  ✓ {email}  ({n} fixture{'s' if n != 1 else ''})")
        except Exception as e:
            print(f"  ✗ {email}  send failed: {e}")
            # Don't record reminder keys for this user — let them be retried
            continue

        # Record reminder keys ONLY for this user's items, after successful send
        if not args.dry_run:
            for m, f in items:
                key = f"{META_PREFIX}:{m['user_id']}:{f['id']}"
                cur.execute("""
                    INSERT INTO meta (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """, (key, now_utc.isoformat()))
            conn.commit()

    if sent_any and args.dry_run:
        print(f"\n(dry-run — meta dedupe rows NOT written; will re-send on next real run)")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
