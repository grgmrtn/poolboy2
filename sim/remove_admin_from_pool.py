"""
sim/remove_admin_from_pool.py — remove the seeded admin user from all
public pools so they don't appear in standings / spy lists.

Admin retains their is_admin=1 flag and login. This only deletes the
pool_members row(s) that put them in a playing pool.

Usage:
    DATABASE_URL='postgresql://...' python3 sim/remove_admin_from_pool.py
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

    cur.execute("SELECT id, email, display_name FROM users WHERE is_admin = 1")
    admins = cur.fetchall()
    if not admins:
        print("No admin users found.")
        return

    print(f"Found {len(admins)} admin user(s):")
    for a in admins:
        print(f"  {a['email']:<30s}  {a['display_name']}")
        cur.execute(
            "SELECT p.name FROM pool_members pm JOIN pools p ON p.id = pm.pool_id "
            "WHERE pm.user_id = %s", (a["id"],)
        )
        pools = [r["name"] for r in cur.fetchall()]
        if pools:
            print(f"    currently in: {pools}")
        else:
            print(f"    currently not in any pools")

    confirm = input("\nRemove admin(s) from ALL pools? (y/N): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    for a in admins:
        # Clean up dependent rows first so the pool_members delete won't fail
        cur.execute("DELETE FROM picks         WHERE user_id = %s", (a["id"],))
        n_picks = cur.rowcount
        cur.execute("DELETE FROM transactions  WHERE user_id = %s", (a["id"],))
        n_txs = cur.rowcount
        cur.execute("DELETE FROM spy_log       WHERE buyer_id = %s OR target_id = %s",
                    (a["id"], a["id"]))
        n_spy = cur.rowcount
        cur.execute("DELETE FROM score_log     WHERE user_id = %s", (a["id"],))
        n_score = cur.rowcount
        cur.execute("DELETE FROM aggregate_spy_log WHERE user_id = %s", (a["id"],))
        n_agg = cur.rowcount
        cur.execute("DELETE FROM pool_members  WHERE user_id = %s", (a["id"],))
        n_mem = cur.rowcount
        print(f"  {a['email']}: picks={n_picks}, txs={n_txs}, spies={n_spy}, "
              f"score_log={n_score}, agg_spies={n_agg}, memberships={n_mem}")

    conn.commit()
    cur.close()
    conn.close()
    print("\nDone. Admin user(s) still exist (can still log in) but are no longer in any pool.")


if __name__ == "__main__":
    main()
