"""
sim/remove_user_from_pool.py — fully remove a user from a single pool.

Deletes all of the user's pool-scoped data: membership, picks, score_log
rows, transactions, spy_log entries (both as buyer and target), and
aggregate_spy_log. The users row itself is preserved so the account can
still log in.

Default is DRY-RUN: prints the row counts per table that would be deleted.
Add --execute to actually perform the deletion (one transaction, rolled
back on any error).

Usage:
    DATABASE_URL='...' python3 sim/remove_user_from_pool.py \\
        --email admin@pool.local --pool 'WC26 Pool'

    # then, if the preview looks right:
    DATABASE_URL='...' python3 sim/remove_user_from_pool.py \\
        --email admin@pool.local --pool 'WC26 Pool' --execute
"""
import os
import sys
import argparse
import psycopg2
import psycopg2.extras


TABLES_POOL_SCOPED = [
    # (table_name, where_clause, params_template)
    # Order matters: child rows before parent rows.
    ("score_log",         "user_id = %s AND pool_id = %s"),
    ("transactions",      "user_id = %s AND pool_id = %s"),
    ("aggregate_spy_log", "user_id = %s AND pool_id = %s"),
    # spy_log: user appears as either buyer or target. Two passes.
    ("spy_log",           "buyer_id  = %s AND pool_id = %s"),
    ("spy_log",           "target_id = %s AND pool_id = %s"),
    ("picks",             "user_id = %s AND pool_id = %s"),
    ("pool_members",      "user_id = %s AND pool_id = %s"),
]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--email",   required=True, help="email of the user to remove")
    p.add_argument("--pool",    required=True, help="pool name (exact match)")
    p.add_argument("--execute", action="store_true",
                   help="actually delete. Without this, runs as a preview.")
    args = p.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL not set"); sys.exit(1)

    conn = psycopg2.connect(url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id, email, display_name FROM users WHERE email = %s", (args.email,))
    user = cur.fetchone()
    if not user:
        print(f"ERROR: no user with email {args.email!r}"); sys.exit(1)

    cur.execute("SELECT id, name FROM pools WHERE name = %s", (args.pool,))
    pool = cur.fetchone()
    if not pool:
        print(f"ERROR: no pool named {args.pool!r}"); sys.exit(1)

    uid, pid = user["id"], pool["id"]
    print(f"\nuser:  {user['email']}  ({user['display_name']})  id={uid}")
    print(f"pool:  {pool['name']}                              id={pid}\n")

    # Preview counts
    print(f"{'table':<22} {'rows':>6}")
    print("─" * 30)
    total = 0
    for table, where in TABLES_POOL_SCOPED:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", (uid, pid))
        n = cur.fetchone()["n"]
        total += n
        marker = "  ←" if n else ""
        print(f"{table:<22} {n:>6}{marker}")
    print("─" * 30)
    print(f"{'total':<22} {total:>6}\n")

    if total == 0:
        print("Nothing to delete. (User may not be a member of that pool.)")
        return

    if not args.execute:
        print("Preview only (no rows touched). Re-run with --execute to delete.")
        return

    # Execute — single transaction
    try:
        for table, where in TABLES_POOL_SCOPED:
            cur.execute(f"DELETE FROM {table} WHERE {where}", (uid, pid))
            if cur.rowcount:
                print(f"  deleted {cur.rowcount:>4} from {table}")
        conn.commit()
        print(f"\n✓ {user['email']} removed from {pool['name']}.")
    except Exception as e:
        conn.rollback()
        print(f"\n✗ Rolled back — {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
