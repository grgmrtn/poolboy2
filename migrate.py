"""
migrate.py — idempotent schema migration for the economy-based picking system.

Adds new columns and tables without touching existing data.
Safe to run repeatedly — all changes are guarded by existence checks.

Usage:
    python3 migrate.py
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "pool.db")


def _col_exists(conn, table, column):
    """Return True if column already exists in table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn, table):
    """Return True if table exists in the database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate():
    """Apply all pending schema migrations and print a summary."""
    if not os.path.exists(DB_PATH):
        print(f"[migrate] No database found at {DB_PATH} — run app.py first to create it.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # ── pool_members: balance column ───────────────────────────────────────
    if not _col_exists(conn, "pool_members", "balance"):
        conn.execute("ALTER TABLE pool_members ADD COLUMN balance REAL DEFAULT 100")
        print("[migrate] pool_members.balance added")

    # ── scoring_config: economy fields ────────────────────────────────────
    sc_cols = [
        ("starting_balance",                "REAL DEFAULT 100"),
        ("group_win_payout",                "REAL DEFAULT 5"),
        ("group_draw_payout",               "REAL DEFAULT 2"),
        ("group_loss_payout",               "REAL DEFAULT 0"),
        ("spy_base_cost",                   "REAL DEFAULT 1"),
        ("spy_increment",                   "REAL DEFAULT 1"),
        ("knockout_flat_payout_multiplier", "REAL DEFAULT 2"),
    ]
    for col, defn in sc_cols:
        if not _col_exists(conn, "scoring_config", col):
            conn.execute(f"ALTER TABLE scoring_config ADD COLUMN {col} {defn}")
            print(f"[migrate] scoring_config.{col} added")

    # ── picks: bet_amount column ───────────────────────────────────────────
    if not _col_exists(conn, "picks", "bet_amount"):
        conn.execute("ALTER TABLE picks ADD COLUMN bet_amount REAL DEFAULT NULL")
        print("[migrate] picks.bet_amount added")

    # ── transactions table ─────────────────────────────────────────────────
    # Ledger of all balance changes: payouts, spy purchases, bets, adjustments.
    if not _table_exists(conn, "transactions"):
        conn.execute("""
            CREATE TABLE transactions (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL REFERENCES users(id),
                pool_id     TEXT NOT NULL REFERENCES pools(id),
                fixture_id  TEXT REFERENCES fixtures(id),
                type        TEXT NOT NULL CHECK(type IN ('payout','spy','bet','adjustment')),
                amount      REAL NOT NULL,
                description TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("[migrate] transactions table created")

    # ── spy_log table ──────────────────────────────────────────────────────
    # Records who has paid to reveal whose pick on which fixture.
    if not _table_exists(conn, "spy_log"):
        conn.execute("""
            CREATE TABLE spy_log (
                id          TEXT PRIMARY KEY,
                buyer_id    TEXT NOT NULL REFERENCES users(id),
                target_id   TEXT NOT NULL REFERENCES users(id),
                pool_id     TEXT NOT NULL REFERENCES pools(id),
                fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
                cost        REAL NOT NULL,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("[migrate] spy_log table created")

    conn.commit()
    conn.close()
    print("[migrate] Migration complete.")


if __name__ == "__main__":
    migrate()
