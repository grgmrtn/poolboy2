"""
sim/cleanup_smoke_users.py — remove smoke_<nonce>@test.local users + their data.

Run after smoke_test_prod.py whenever you want to clean up the throwaway
accounts the test leaves behind. Safe to run anytime; only matches users
whose email starts with 'smoke_' and ends with '@test.local'.

Usage:
    DATABASE_URL='postgresql://...' python3 sim/cleanup_smoke_users.py
"""
import os
import sys
import psycopg2
import psycopg2.extras


def main():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: $DATABASE_URL not set"); sys.exit(1)

    conn = psycopg2.connect(url)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Identify smoke users
    cur.execute("""
        SELECT id, email, display_name, created_at
        FROM users
        WHERE email LIKE 'smoke\\_%@test.local' ESCAPE '\\'
        ORDER BY created_at
    """)
    smokes = cur.fetchall()

    if not smokes:
        print("No smoke users found. Nothing to do.")
        return

    print(f"Found {len(smokes)} smoke user(s):")
    for u in smokes:
        print(f"  {u['email']:<40s}  {u['display_name']:<10s}  {u['created_at']}")

    confirm = input(f"\nDelete these {len(smokes)} user(s) and all their picks/transactions/login_log/pool_members? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    smoke_ids = [u["id"] for u in smokes]

    # Delete in dependency order, all in one transaction
    deletes = [
        ("picks",        "user_id = ANY(%s)"),
        ("transactions", "user_id = ANY(%s)"),
        ("spy_log",      "buyer_id = ANY(%s) OR target_id = ANY(%s)"),
        ("score_log",    "user_id = ANY(%s)"),
        ("pool_members", "user_id = ANY(%s)"),
        ("login_log",    "user_id = ANY(%s)"),
        ("users",        "id = ANY(%s)"),
    ]
    try:
        for table, where in deletes:
            if where.count("%s") == 2:
                cur.execute(f"DELETE FROM {table} WHERE {where}", (smoke_ids, smoke_ids))
            else:
                cur.execute(f"DELETE FROM {table} WHERE {where}", (smoke_ids,))
            print(f"  {table:<14s} {cur.rowcount} row(s) deleted")
        conn.commit()
        print("\nDone.")
    except Exception as e:
        conn.rollback()
        print(f"\nFAILED, rolled back: {e}")
        sys.exit(1)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
