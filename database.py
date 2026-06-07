"""
database.py — all database setup and query helpers.

Uses Python's built-in sqlite3 module (no external ORM needed).
The database file is created automatically at first run.
"""

import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "pool.db")

_USE_POSTGRES = bool(os.environ.get("DATABASE_URL"))

# Catch integrity violations from whichever backend is active.
try:
    import psycopg2 as _pg
    _IntegrityError = (sqlite3.IntegrityError, _pg.IntegrityError)
except ImportError:
    _IntegrityError = (sqlite3.IntegrityError,)


class _PGConn:
    """Wraps a psycopg2 connection to expose the same API as sqlite3."""
    def __init__(self, raw):
        import psycopg2.extras
        self._conn = raw
        self._cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=()):
        # Postgres uses %s for parameters and treats %% as a literal %.
        # Escape any literal % in the SQL first so LIKE patterns like 'Group %'
        # survive psycopg2's parameter substitution, then convert sqlite-style
        # ? to %s. Order matters: escape % before introducing our own %s.
        sql = sql.replace("%", "%%").replace("?", "%s")
        self._cur.execute(sql, params or ())
        return self._cur

    def cursor(self):
        return self  # init_db uses c = conn.cursor(); c.execute(...)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._cur.close()
        self._conn.close()


def get_db():
    """
    Open a database connection.
    Returns a psycopg2-backed _PGConn when DATABASE_URL is set, otherwise sqlite3.
    Both expose the same conn.execute() / .commit() / .close() interface.
    """
    if _USE_POSTGRES:
        import psycopg2
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        return _PGConn(conn)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """
    Create all tables if they don't exist yet.
    Safe to run multiple times — won't wipe existing data.
    """
    if not _USE_POSTGRES:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    c = conn

    # Stores everyone who can log in. is_admin=1 unlocks the /admin page.
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # A pool is a competition group. is_public=1 makes it visible to everyone.
    c.execute("""
        CREATE TABLE IF NOT EXISTS pools (
            id                   TEXT PRIMARY KEY,
            name                 TEXT NOT NULL,
            description          TEXT,
            is_public            INTEGER DEFAULT 1,
            entry_fee            TEXT,
            payment_instructions TEXT,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Join table: which users belong to which pools.
    # balance is the player's current economy balance in this pool.
    c.execute("""
        CREATE TABLE IF NOT EXISTS pool_members (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id),
            pool_id    TEXT NOT NULL REFERENCES pools(id),
            has_paid   INTEGER DEFAULT 0,
            balance    REAL DEFAULT 100,
            joined_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, pool_id)
        )
    """)

    # One row per match. home_score/away_score/result are NULL until played.
    # home_odds/away_odds are NULL until an admin sets them (KO only); when NULL,
    # process_fixture_result falls back to scoring_config.knockout_flat_payout_multiplier.
    c.execute("""
        CREATE TABLE IF NOT EXISTS fixtures (
            id             TEXT PRIMARY KEY,
            home_team      TEXT,
            away_team      TEXT,
            home_flag_code TEXT,
            away_flag_code TEXT,
            kick_off       TEXT,
            stage          TEXT,
            home_score     INTEGER,
            away_score     INTEGER,
            result         TEXT,
            home_odds      REAL,
            away_odds      REAL
        )
    """)

    # A user's prediction for one fixture within a specific pool.
    # predicted_result: "H" = home win, "A" = away win, "D" = draw.
    # bet_amount: used for knockout stage picks only (deducted at submission).
    c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id               TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL REFERENCES users(id),
            pool_id          TEXT NOT NULL REFERENCES pools(id),
            fixture_id       TEXT NOT NULL REFERENCES fixtures(id),
            predicted_result TEXT NOT NULL CHECK(predicted_result IN ('H','A','D')),
            bet_amount       REAL DEFAULT NULL,
            submitted_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, pool_id, fixture_id)
        )
    """)

    # Admin sets how many points a correct win/draw/loss prediction scores.
    # New rows are inserted rather than updating, preserving history.
    # Economy fields added by migrate.py; defaults applied in get_scoring_config().
    c.execute("""
        CREATE TABLE IF NOT EXISTS scoring_config (
            id                              TEXT PRIMARY KEY,
            points_win                      INTEGER NOT NULL DEFAULT 3,
            points_draw                     INTEGER NOT NULL DEFAULT 1,
            points_loss                     INTEGER NOT NULL DEFAULT 0,
            updated_at                      TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_by                      TEXT REFERENCES users(id),
            starting_balance                REAL DEFAULT 100,
            group_win_payout                REAL DEFAULT 5,
            group_draw_payout               REAL DEFAULT 2,
            group_loss_payout               REAL DEFAULT 0,
            spy_base_cost                   REAL DEFAULT 1,
            spy_increment                   REAL DEFAULT 1,
            knockout_flat_payout_multiplier REAL DEFAULT 2
        )
    """)

    # Kept for backward compatibility — still populated by old calculate_scores_for_fixture
    # calls but new process_fixture_result uses the transactions table instead.
    c.execute("""
        CREATE TABLE IF NOT EXISTS score_log (
            id              TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL REFERENCES users(id),
            pool_id         TEXT NOT NULL REFERENCES pools(id),
            pick_id         TEXT NOT NULL REFERENCES picks(id),
            points_awarded  INTEGER NOT NULL DEFAULT 0,
            calculated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pick_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Ledger of all economy balance changes in the pool.
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
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

    # Records who has paid to reveal whose pick on which fixture.
    c.execute("""
        CREATE TABLE IF NOT EXISTS spy_log (
            id          TEXT PRIMARY KEY,
            buyer_id    TEXT NOT NULL REFERENCES users(id),
            target_id   TEXT NOT NULL REFERENCES users(id),
            pool_id     TEXT NOT NULL REFERENCES pools(id),
            fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
            cost        REAL NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # One row per successful login — drives the daily admin digest.
    # Append-only; not pruned. Cheap to keep at this scale (10s of rows/day).
    c.execute("""
        CREATE TABLE IF NOT EXISTS login_log (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL REFERENCES users(id),
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrations: add columns to existing tables that pre-date this schema version.
    if _USE_POSTGRES:
        for sql in [
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS pool_id TEXT",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS round_type TEXT",
            "ALTER TABLE pools ADD COLUMN IF NOT EXISTS entry_fee TEXT",
            "ALTER TABLE pools ADD COLUMN IF NOT EXISTS payment_instructions TEXT",
            "ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS has_paid INTEGER DEFAULT 0",
            "ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS balance REAL DEFAULT 100",
            "ALTER TABLE picks ADD COLUMN IF NOT EXISTS bet_amount REAL DEFAULT NULL",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS starting_balance REAL DEFAULT 100",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS group_win_payout REAL DEFAULT 5",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS group_draw_payout REAL DEFAULT 2",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS group_loss_payout REAL DEFAULT 0",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS spy_base_cost REAL DEFAULT 1",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS spy_increment REAL DEFAULT 1",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS knockout_flat_payout_multiplier REAL DEFAULT 2",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS home_odds REAL",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS away_odds REAL",
        ]:
            c.execute(sql)
    else:
        migrations = [
            ("scoring_config", "pool_id TEXT"),
            ("scoring_config", "round_type TEXT"),
            ("pools",          "entry_fee TEXT"),
            ("pools",          "payment_instructions TEXT"),
            ("pool_members",   "has_paid INTEGER DEFAULT 0"),
            ("pool_members",   "balance REAL DEFAULT 100"),
            ("picks",          "bet_amount REAL DEFAULT NULL"),
            ("scoring_config", "starting_balance REAL DEFAULT 100"),
            ("scoring_config", "group_win_payout REAL DEFAULT 5"),
            ("scoring_config", "group_draw_payout REAL DEFAULT 2"),
            ("scoring_config", "group_loss_payout REAL DEFAULT 0"),
            ("scoring_config", "spy_base_cost REAL DEFAULT 1"),
            ("scoring_config", "spy_increment REAL DEFAULT 1"),
            ("scoring_config", "knockout_flat_payout_multiplier REAL DEFAULT 2"),
            ("fixtures",       "home_odds REAL"),
            ("fixtures",       "away_odds REAL"),
        ]
        for table, col_def in migrations:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except Exception:
                pass

    conn.commit()
    conn.close()
    print(f"[db] Database ready at {DB_PATH}")


# ── Stage helpers ──────────────────────────────────────────────────────────

_KNOCKOUT_STAGES = frozenset({
    'Round of 32', 'Round of 16', 'Quarter-Finals',
    'Semi-Finals', 'Third Place', 'Final',
})


def is_knockout_stage(stage):
    """Return True if the given stage name is a knockout round (not group stage)."""
    return (stage or '').strip() in _KNOCKOUT_STAGES


def _round_type_for_stage(stage):
    return 'knockout' if is_knockout_stage(stage) else 'group_stage'


# ── Meta helpers ───────────────────────────────────────────────────────────

def get_meta(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_meta(key, value):
    conn = get_db()
    conn.execute("""
        INSERT INTO meta (key, value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


# ── User helpers ───────────────────────────────────────────────────────────

def get_user_by_email(email):
    """Look up a user row by email address. Returns None if not found."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return user


def get_user_by_id(user_id):
    """Look up a user row by their UUID. Returns None if not found."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def record_login(user_id):
    """Insert a login_log row. Best-effort: swallows any DB error."""
    import uuid as _uuid
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO login_log (id, user_id) VALUES (?,?)",
            (str(_uuid.uuid4()), user_id)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def create_user(user_id, display_name, email, password_hash, is_admin=0):
    """Insert a new user into the database."""
    conn = get_db()
    conn.execute(
        "INSERT INTO users (id, display_name, email, password_hash, is_admin) VALUES (?,?,?,?,?)",
        (user_id, display_name, email, password_hash, is_admin)
    )
    conn.commit()
    conn.close()


def get_user_total_score(user_id, pool_id=None):
    """
    Return the total points a user has earned (from score_log, for backward compat).
    If pool_id is given, returns score within that pool only.
    """
    conn = get_db()
    if pool_id:
        row = conn.execute(
            "SELECT COALESCE(SUM(points_awarded), 0) AS total FROM score_log WHERE user_id=? AND pool_id=?",
            (user_id, pool_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(points_awarded), 0) AS total FROM score_log WHERE user_id=?",
            (user_id,)
        ).fetchone()
    conn.close()
    return row["total"]


# ── Pool helpers ───────────────────────────────────────────────────────────

def get_all_public_pools():
    """Return all pools marked is_public=1, sorted by name."""
    conn = get_db()
    pools = conn.execute("SELECT * FROM pools WHERE is_public=1 ORDER BY name").fetchall()
    conn.close()
    return pools


def get_pools_for_user(user_id):
    """Return all pools the user belongs to, plus their has_paid and balance."""
    conn = get_db()
    pools = conn.execute("""
        SELECT p.*, pm.has_paid,
               COALESCE(pm.balance, 100.0) AS balance
        FROM pools p
        JOIN pool_members pm ON pm.pool_id = p.id
        WHERE pm.user_id = ? AND p.id IS NOT NULL
        ORDER BY p.name
    """, (user_id,)).fetchall()
    conn.close()
    return pools


def is_pool_member(user_id, pool_id):
    """Return True if the user is already a member of the given pool."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM pool_members WHERE user_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchone()
    conn.close()
    return row is not None


def join_pool(member_id, user_id, pool_id, has_paid=0, balance=100.0):
    """
    Add user to pool.

    balance: starting balance — defaults to 100 but callers should pass the
    pool's scoring_config.starting_balance for correctness.
    """
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO pool_members (id, user_id, pool_id, has_paid, balance) VALUES (?,?,?,?,?)",
            (member_id, user_id, pool_id, has_paid, balance)
        )
        conn.commit()
    except _IntegrityError:
        pass
    finally:
        conn.close()


def get_pool_membership(user_id, pool_id):
    """Return the pool_members row for this user+pool as a dict, or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pool_members WHERE user_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_member_paid(user_id, pool_id, paid):
    conn = get_db()
    conn.execute(
        "UPDATE pool_members SET has_paid=? WHERE user_id=? AND pool_id=?",
        (paid, user_id, pool_id)
    )
    conn.commit()
    conn.close()


def get_pool_members(pool_id):
    """Return all members of a pool with their payment status, ordered by name."""
    conn = get_db()
    rows = conn.execute("""
        SELECT u.id, u.display_name, u.email, pm.has_paid, pm.joined_at
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.pool_id = ?
        ORDER BY u.display_name
    """, (pool_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pool_by_id(pool_id):
    """Fetch a single pool row by its ID."""
    conn = get_db()
    pool = conn.execute("SELECT * FROM pools WHERE id=?", (pool_id,)).fetchone()
    conn.close()
    return pool


def create_pool(pool_id, name, description, is_public=1,
                entry_fee=None, payment_instructions=None):
    """Insert a new pool."""
    conn = get_db()
    conn.execute(
        "INSERT INTO pools (id, name, description, is_public, entry_fee, payment_instructions)"
        " VALUES (?,?,?,?,?,?)",
        (pool_id, name, description, is_public, entry_fee or None, payment_instructions or None)
    )
    conn.commit()
    conn.close()


def get_pool_leaderboard(pool_id):
    """
    Return all members of a pool ranked by current balance, highest first.
    Each row includes display_name, email, and balance.
    Falls back to 100.0 if balance column is NULL (pre-migration rows).
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT u.display_name, u.email,
               COALESCE(pm.balance, 100.0) AS balance
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.pool_id = ?
        ORDER BY balance DESC
    """, (pool_id,)).fetchall()
    conn.close()
    return rows


# ── Fixture helpers ────────────────────────────────────────────────────────

def upsert_fixture(fixture):
    """
    Insert or update a fixture row.

    fixture: a dict with keys: id, home_team, away_team, home_flag_code,
             away_flag_code, kick_off, stage
    """
    conn = get_db()
    conn.execute("""
        INSERT INTO fixtures (id, home_team, away_team, home_flag_code, away_flag_code, kick_off, stage)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            home_team      = excluded.home_team,
            away_team      = excluded.away_team,
            home_flag_code = excluded.home_flag_code,
            away_flag_code = excluded.away_flag_code,
            kick_off       = excluded.kick_off,
            stage          = excluded.stage
    """, (
        fixture["id"],
        fixture["home_team"],
        fixture["away_team"],
        fixture.get("home_flag_code"),
        fixture.get("away_flag_code"),
        fixture.get("kick_off"),
        fixture.get("stage"),
    ))
    conn.commit()
    conn.close()


def get_fixtures():
    """Return all fixtures ordered by kick-off time."""
    conn = get_db()
    fixtures = conn.execute("SELECT * FROM fixtures ORDER BY kick_off").fetchall()
    conn.close()
    return fixtures


def set_fixture_odds(fixture_id, home_odds, away_odds):
    """
    Update the per-fixture H/A odds. Used by /admin to override the
    flat knockout multiplier for individual KO matches. Either side can
    be NULL to leave it unset and inherit the global multiplier.
    """
    conn = get_db()
    conn.execute(
        "UPDATE fixtures SET home_odds=?, away_odds=? WHERE id=?",
        (home_odds, away_odds, fixture_id)
    )
    conn.commit()
    conn.close()


def update_fixture_result(fixture_id, home_score, away_score):
    """
    Record the final score for a fixture and derive the result code.
    result codes: "H" = home win, "A" = away win, "D" = draw.
    """
    if home_score > away_score:
        result = "H"
    elif away_score > home_score:
        result = "A"
    else:
        result = "D"

    conn = get_db()
    conn.execute(
        "UPDATE fixtures SET home_score=?, away_score=?, result=? WHERE id=?",
        (home_score, away_score, result, fixture_id)
    )
    conn.commit()
    conn.close()
    return result


# ── Pick helpers ───────────────────────────────────────────────────────────

def upsert_pick(pick_id, user_id, pool_id, fixture_id, predicted_result, bet_amount=None):
    """
    Save or update a user's pick for a fixture.

    bet_amount: only used for knockout fixtures; NULL for group stage.
    If the pick already exists, overwrites predicted_result, bet_amount, and
    submitted_at while keeping the original row id.
    """
    conn = get_db()
    conn.execute("""
        INSERT INTO picks (id, user_id, pool_id, fixture_id, predicted_result, bet_amount)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id, pool_id, fixture_id) DO UPDATE SET
            predicted_result = excluded.predicted_result,
            bet_amount       = excluded.bet_amount,
            submitted_at     = CURRENT_TIMESTAMP
    """, (pick_id, user_id, pool_id, fixture_id, predicted_result, bet_amount))
    conn.commit()
    conn.close()


def get_picks_for_user_in_pool(user_id, pool_id):
    """
    Return a dict mapping fixture_id → predicted_result for quick lookup.
    e.g. { "fixture-abc": "H", "fixture-xyz": "D" }
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id, predicted_result FROM picks WHERE user_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchall()
    conn.close()
    return {row["fixture_id"]: row["predicted_result"] for row in rows}


def get_picks_full_for_user_in_pool(user_id, pool_id):
    """
    Return a dict mapping fixture_id → {predicted_result, bet_amount}.
    Used on the pool page to render the user's own picks including KO bet amounts.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id, predicted_result, bet_amount FROM picks WHERE user_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchall()
    conn.close()
    return {r["fixture_id"]: {"predicted_result": r["predicted_result"],
                               "bet_amount": r["bet_amount"]}
            for r in rows}


# ── Scoring config helpers ─────────────────────────────────────────────────

_CONFIG_DEFAULTS = {
    "points_win":                      3,
    "points_draw":                     1,
    "points_loss":                     0,
    "starting_balance":                100.0,
    "group_win_payout":                5.0,
    "group_draw_payout":               2.0,
    "group_loss_payout":               0.0,
    "spy_base_cost":                   1.0,
    "spy_increment":                   1.0,
    "knockout_flat_payout_multiplier": 2.0,
}


def _apply_config_defaults(cfg):
    """Fill in missing/NULL economy fields on a scoring config dict."""
    for k, v in _CONFIG_DEFAULTS.items():
        if k not in cfg or cfg[k] is None:
            cfg[k] = v
    return cfg


def get_scoring_config(pool_id=None, stage=None):
    """
    Return the scoring config for a pool/stage combination.
    Falls back: pool+round_type → pool (any round) → global → hardcoded defaults.
    All economy fields are guaranteed non-NULL via _apply_config_defaults.
    """
    round_type = _round_type_for_stage(stage)
    conn = get_db()

    if pool_id:
        row = conn.execute("""
            SELECT * FROM scoring_config WHERE pool_id=? AND round_type=?
            ORDER BY updated_at DESC LIMIT 1
        """, (pool_id, round_type)).fetchone()
        if row:
            conn.close(); return _apply_config_defaults(dict(row))

        row = conn.execute("""
            SELECT * FROM scoring_config WHERE pool_id=? AND round_type IS NULL
            ORDER BY updated_at DESC LIMIT 1
        """, (pool_id,)).fetchone()
        if row:
            conn.close(); return _apply_config_defaults(dict(row))

    row = conn.execute("""
        SELECT * FROM scoring_config WHERE pool_id IS NULL AND round_type IS NULL
        ORDER BY updated_at DESC LIMIT 1
    """).fetchone()
    conn.close()
    if row:
        return _apply_config_defaults(dict(row))
    return dict(_CONFIG_DEFAULTS)


def get_active_scoring_config():
    return get_scoring_config()


def save_scoring_config(config_id, points_win, points_draw, points_loss, updated_by,
                        pool_id=None, round_type=None,
                        starting_balance=None, group_win_payout=None,
                        group_draw_payout=None, group_loss_payout=None,
                        spy_base_cost=None, spy_increment=None,
                        knockout_flat_payout_multiplier=None):
    """
    Insert a new scoring_config row (history is preserved, not updated in-place).
    Economy fields default to None; the DB column defaults or _apply_config_defaults
    fill them in on read.
    """
    conn = get_db()
    conn.execute("""
        INSERT INTO scoring_config (
            id, pool_id, round_type, points_win, points_draw, points_loss, updated_by,
            starting_balance, group_win_payout, group_draw_payout, group_loss_payout,
            spy_base_cost, spy_increment, knockout_flat_payout_multiplier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (config_id, pool_id, round_type, points_win, points_draw, points_loss, updated_by,
          starting_balance, group_win_payout, group_draw_payout, group_loss_payout,
          spy_base_cost, spy_increment, knockout_flat_payout_multiplier))
    conn.commit()
    conn.close()


# ── User helpers (extended) ────────────────────────────────────────────────

def get_all_users():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    conn.close()
    return [dict(u) for u in users]


def set_user_admin(user_id, is_admin):
    conn = get_db()
    conn.execute("UPDATE users SET is_admin=? WHERE id=?", (is_admin, user_id))
    conn.commit()
    conn.close()


# ── Economy helpers ────────────────────────────────────────────────────────

def get_member_balance(user_id, pool_id):
    """Return the current balance for a user in a pool. Defaults to 100 if missing."""
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(balance, 100.0) AS balance FROM pool_members WHERE user_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchone()
    conn.close()
    return row["balance"] if row else 0.0


def _write_transaction(conn, tx_id, user_id, pool_id, fixture_id, tx_type, amount, description):
    """
    Write one transaction row using an already-open connection.

    Called from within multi-step atomic operations (spy purchase, result processing).
    Caller is responsible for commit.
    """
    conn.execute("""
        INSERT INTO transactions (id, user_id, pool_id, fixture_id, type, amount, description)
        VALUES (?,?,?,?,?,?,?)
    """, (tx_id, user_id, pool_id, fixture_id, tx_type, amount, description))


def _update_balance(conn, user_id, pool_id, delta):
    """Add delta (positive or negative) to a member's balance. Caller commits."""
    conn.execute(
        "UPDATE pool_members SET balance = COALESCE(balance, 100) + ? WHERE user_id=? AND pool_id=?",
        (delta, user_id, pool_id)
    )


def get_spy_count_for_user_in_pool(user_id, pool_id):
    """
    Return total number of spy purchases this user has made in this pool (since joining).
    Used to compute the cost of the next spy: base_cost + increment × count.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM spy_log WHERE buyer_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_spy_set_for_user_in_pool(user_id, pool_id):
    """
    Return a frozenset of (fixture_id, target_user_id) pairs representing all
    spy accesses this user has already purchased in this pool.
    Used on page load to decide which picks to render vs. hide.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id, target_id FROM spy_log WHERE buyer_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchall()
    conn.close()
    return frozenset((r["fixture_id"], r["target_id"]) for r in rows)


def has_spied(buyer_id, target_id, pool_id, fixture_id):
    """Return True if the buyer has already purchased spy access for this target+fixture."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM spy_log WHERE buyer_id=? AND target_id=? AND pool_id=? AND fixture_id=?",
        (buyer_id, target_id, pool_id, fixture_id)
    ).fetchone()
    conn.close()
    return row is not None


def get_pick_for_user_fixture(user_id, pool_id, fixture_id):
    """Fetch a single pick dict or None — used by the spy endpoint after a free reveal."""
    conn = get_db()
    row = conn.execute(
        "SELECT predicted_result, bet_amount FROM picks WHERE user_id=? AND pool_id=? AND fixture_id=?",
        (user_id, pool_id, fixture_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def record_spy(spy_id, tx_id, buyer_id, target_id, pool_id, fixture_id, cost):
    """
    Atomically purchase spy access for one target on one fixture.

    Writes a spy_log row, a 'spy' transaction, and deducts cost from the buyer's
    balance. Raises ValueError if the buyer already spied on this target+fixture.

    Returns the target's pick dict {predicted_result, bet_amount} or None if
    the target has not yet placed a pick for this fixture.
    """
    conn = get_db()

    # Idempotency guard
    existing = conn.execute(
        "SELECT id FROM spy_log WHERE buyer_id=? AND target_id=? AND pool_id=? AND fixture_id=?",
        (buyer_id, target_id, pool_id, fixture_id)
    ).fetchone()
    if existing:
        # Already purchased — return pick for free without charging again
        pick = conn.execute(
            "SELECT predicted_result, bet_amount FROM picks WHERE user_id=? AND pool_id=? AND fixture_id=?",
            (target_id, pool_id, fixture_id)
        ).fetchone()
        conn.close()
        return dict(pick) if pick else None

    conn.execute("""
        INSERT INTO spy_log (id, buyer_id, target_id, pool_id, fixture_id, cost)
        VALUES (?,?,?,?,?,?)
    """, (spy_id, buyer_id, target_id, pool_id, fixture_id, cost))

    _write_transaction(conn, tx_id, buyer_id, pool_id, fixture_id, "spy", -cost,
                       f"Spy purchase — fixture {fixture_id[:8]}")
    _update_balance(conn, buyer_id, pool_id, -cost)

    pick = conn.execute(
        "SELECT predicted_result, bet_amount FROM picks WHERE user_id=? AND pool_id=? AND fixture_id=?",
        (target_id, pool_id, fixture_id)
    ).fetchone()

    conn.commit()
    conn.close()
    return dict(pick) if pick else None


def process_fixture_result(fixture_id):
    """
    Award or deduct economy balances + score_log points for all picks on a completed fixture.

    Group stage: credits group_win_payout / group_draw_payout / group_loss_payout
    (from scoring_config) to each member's balance and writes a 'payout' transaction.

    Knockout stage: credits bet_amount × knockout_flat_payout_multiplier for
    correct predictions (bet was already deducted at pick submission time).

    Also writes a score_log row per pick (points_win / points_draw / points_loss
    from scoring_config) so the /pool/<id>/stats points-over-time chart renders.

    Result corrections are handled by reversing any existing payout transactions
    and score_log rows for this fixture before re-deriving from the new result.
    Returns: number of picks processed.
    """
    import uuid as _uuid
    conn = get_db()

    fixture = conn.execute(
        "SELECT result, stage, home_odds, away_odds FROM fixtures WHERE id=?", (fixture_id,)
    ).fetchone()
    if not fixture or not fixture["result"]:
        conn.close()
        return 0

    actual = fixture["result"]
    stage  = fixture["stage"] or ""
    knockout = is_knockout_stage(stage)
    home_odds = fixture["home_odds"]
    away_odds = fixture["away_odds"]

    # Reverse prior payouts (handles admin result corrections): undo the balance
    # impact and delete the transactions so re-processing produces correct totals.
    prior = conn.execute(
        "SELECT user_id, pool_id, amount FROM transactions WHERE type='payout' AND fixture_id=?",
        (fixture_id,)
    ).fetchall()
    for r in prior:
        _update_balance(conn, r["user_id"], r["pool_id"], -r["amount"])
    conn.execute("DELETE FROM transactions WHERE type='payout' AND fixture_id=?", (fixture_id,))
    # Same for score_log — drop prior rows so re-scoring works after a correction.
    conn.execute(
        "DELETE FROM score_log WHERE pick_id IN (SELECT id FROM picks WHERE fixture_id=?)",
        (fixture_id,)
    )

    picks = conn.execute(
        "SELECT id, user_id, pool_id, predicted_result, bet_amount FROM picks WHERE fixture_id=?",
        (fixture_id,)
    ).fetchall()

    processed = 0
    for pick in picks:
        config = get_scoring_config(pick["pool_id"], stage)
        correct = pick["predicted_result"] == actual

        # Points (score_log) — used by the stats timeline.
        if correct and actual == "D":
            points = config.get("points_draw", config["points_win"])
        elif correct:
            points = config["points_win"]
        else:
            points = config["points_loss"]
        conn.execute("""
            INSERT INTO score_log (id, user_id, pool_id, pick_id, points_awarded)
            VALUES (?,?,?,?,?)
        """, (str(_uuid.uuid4()), pick["user_id"], pick["pool_id"], pick["id"], points))

        # Economy (transactions / balances).
        if knockout:
            if not correct:
                processed += 1  # Bet already deducted at submission — no further action
                continue
            bet = pick["bet_amount"] or 0.0
            # Per-fixture odds take precedence over the global flat multiplier.
            # A NULL on the winning side falls back to the config default so
            # admins can leave odds unset and still get sensible payouts.
            if actual == "H" and home_odds is not None:
                mult = home_odds
            elif actual == "A" and away_odds is not None:
                mult = away_odds
            else:
                mult = config["knockout_flat_payout_multiplier"]
            payout = round(bet * mult, 2)
            desc = (f"KO win — {pick['predicted_result']} correct"
                    f" · ${bet:.2f} × {mult} = ${payout:.2f}")
        else:
            if correct and actual == "D":
                payout = config["group_draw_payout"]
                desc = f"Group draw — correct pick · +${payout:.2f}"
            elif correct:
                payout = config["group_win_payout"]
                desc = f"Group win — correct pick · +${payout:.2f}"
            else:
                payout = config["group_loss_payout"]
                desc = f"Group miss — incorrect pick · {payout:+.2f}"

        _write_transaction(conn, str(_uuid.uuid4()), pick["user_id"], pick["pool_id"],
                           fixture_id, "payout", payout, desc)
        _update_balance(conn, pick["user_id"], pick["pool_id"], payout)
        processed += 1

    conn.commit()
    conn.close()
    return processed


def apply_balance_adjustment(tx_id, user_id, pool_id, amount, description):
    """
    Manually adjust a pool member's balance (admin action).

    Writes an 'adjustment' transaction and updates pool_members.balance.
    amount: positive to add, negative to deduct.
    """
    conn = get_db()
    _write_transaction(conn, tx_id, user_id, pool_id, None, "adjustment", amount, description)
    _update_balance(conn, user_id, pool_id, amount)
    conn.commit()
    conn.close()


def get_pool_members_with_balances(pool_id):
    """
    Return all pool members with balance and transaction count, for the admin overview.
    Each row: {id, display_name, email, balance, tx_count}.
    """
    conn = get_db()
    # Postgres requires every non-aggregate selected column to appear in GROUP BY.
    # SQLite is loose about this; keep the explicit list so both backends agree.
    rows = conn.execute("""
        SELECT u.id, u.display_name, u.email,
               COALESCE(pm.balance, 100.0) AS balance,
               COUNT(t.id) AS tx_count
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        LEFT JOIN transactions t ON t.user_id = pm.user_id AND t.pool_id = pm.pool_id
        WHERE pm.pool_id = ?
        GROUP BY u.id, u.display_name, u.email, pm.balance
        ORDER BY balance DESC
    """, (pool_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_transactions(user_id, pool_id):
    """
    Return all transactions for a user in a pool, most recent first.
    Each row: {id, type, amount, description, created_at, fixture_id}.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT t.*, f.home_team, f.away_team
        FROM transactions t
        LEFT JOIN fixtures f ON f.id = t.fixture_id
        WHERE t.user_id=? AND t.pool_id=?
        ORDER BY t.created_at DESC
    """, (user_id, pool_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Group standings ────────────────────────────────────────────────────────

def _head_to_head_sort(teams, group_fixture_rows):
    """
    Sort a subset of tied teams using head-to-head results (FIFA steps 4–6).

    teams: list of team stat dicts (all have equal Pts, GD, GF globally)
    group_fixture_rows: all raw fixture dicts for this group (used to extract H2H)

    FIFA steps 7 (fair play) and 8 (FIFA ranking) are not implementable from
    the free football-data.org API tier — card data and ranking data require
    a paid subscription. Unresolved ties fall back to alphabetical team name.
    """
    team_names = {t["team"] for t in teams}
    h2h = {t["team"]: {"points": 0, "gd": 0, "gf": 0} for t in teams}

    for f in group_fixture_rows:
        home, away = f.get("home_team"), f.get("away_team")
        if home not in team_names or away not in team_names:
            continue
        if not f.get("result"):
            continue
        hs = f.get("home_score") or 0
        as_ = f.get("away_score") or 0
        h2h[home]["gf"] += hs
        h2h[home]["gd"] += hs - as_
        h2h[away]["gf"] += as_
        h2h[away]["gd"] += as_ - hs
        if f["result"] == "H":
            h2h[home]["points"] += 3
        elif f["result"] == "A":
            h2h[away]["points"] += 3
        else:
            h2h[home]["points"] += 1
            h2h[away]["points"] += 1

    return sorted(teams, key=lambda t: (
        -h2h[t["team"]]["points"],
        -h2h[t["team"]]["gd"],
        -h2h[t["team"]]["gf"],
        t["team"],  # alphabetical fallback (FIFA ranking not available)
    ))


def _sort_group_teams(teams, group_fixture_rows):
    """
    Sort group teams by FIFA 2026 tiebreaker order.

    Steps 1–3 (global): Pts → GD → GF.
    Steps 4–6 (head-to-head among remaining ties): H2H Pts → H2H GD → H2H GF.
    Steps 7–8 not available; ties resolved alphabetically.
    """
    def primary_key(t):
        return (-t["points"], -t["goal_diff"], -t["goals_for"], t["team"])

    sorted_teams = sorted(teams, key=primary_key)

    # Identify consecutive groups that are still tied after steps 1–3
    result = []
    i = 0
    while i < len(sorted_teams):
        j = i + 1
        while j < len(sorted_teams):
            a, b = sorted_teams[i], sorted_teams[j]
            if (a["points"] == b["points"] and
                    a["goal_diff"] == b["goal_diff"] and
                    a["goals_for"] == b["goals_for"]):
                j += 1
            else:
                break

        tied_slice = sorted_teams[i:j]
        if len(tied_slice) > 1:
            tied_slice = _head_to_head_sort(tied_slice, group_fixture_rows)
        result.extend(tied_slice)
        i = j

    return result


def get_group_standings():
    """
    Calculate current group standings from completed fixtures.

    FIFA 2026 format: 12 groups of 4 teams; top 2 per group qualify automatically
    plus 8 best third-place finishers.  Qualification colours are applied in the
    template: 1st/2nd = green, 3rd = amber (potential qualifier), 4th = dim.

    Only includes groups that have at least one completed result.

    Returns a dict: group_name → list of team dicts sorted by FIFA tiebreaker order.
    Each team dict: {team, flag_code, played, won, drawn, lost,
                     goals_for, goals_against, goal_diff, points}
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT stage, home_team, away_team, home_flag_code, away_flag_code,
               home_score, away_score, result
        FROM fixtures
        WHERE result IS NOT NULL AND result != ''
              AND stage LIKE 'Group %'
        ORDER BY stage, kick_off
    """).fetchall()
    conn.close()

    if not rows:
        return {}

    raw_rows = [dict(r) for r in rows]

    # Build stats per group
    groups_stats = {}   # group → {team → stat_dict}
    groups_fixtures = {}  # group → [fixture rows]
    flag_map = {}

    for r in raw_rows:
        group = r["stage"]
        home, away = r["home_team"], r["away_team"]
        hs, as_ = (r["home_score"] or 0), (r["away_score"] or 0)

        flag_map[home] = r["home_flag_code"] or "un"
        flag_map[away] = r["away_flag_code"] or "un"

        groups_fixtures.setdefault(group, []).append(r)

        if group not in groups_stats:
            groups_stats[group] = {}

        def ensure(team):
            if team not in groups_stats[group]:
                groups_stats[group][team] = dict(
                    team=team, played=0, won=0, drawn=0, lost=0,
                    goals_for=0, goals_against=0, goal_diff=0, points=0
                )

        ensure(home)
        ensure(away)

        gs = groups_stats[group]
        gs[home]["played"] += 1
        gs[away]["played"] += 1
        gs[home]["goals_for"] += hs
        gs[home]["goals_against"] += as_
        gs[away]["goals_for"] += as_
        gs[away]["goals_against"] += hs

        result = r["result"]
        if result == "H":
            gs[home]["won"] += 1;   gs[home]["points"] += 3
            gs[away]["lost"] += 1
        elif result == "A":
            gs[away]["won"] += 1;   gs[away]["points"] += 3
            gs[home]["lost"] += 1
        else:
            gs[home]["drawn"] += 1; gs[home]["points"] += 1
            gs[away]["drawn"] += 1; gs[away]["points"] += 1

        gs[home]["goal_diff"] = gs[home]["goals_for"] - gs[home]["goals_against"]
        gs[away]["goal_diff"] = gs[away]["goals_for"] - gs[away]["goals_against"]

    # Sort each group and attach flag codes
    sorted_groups = {}
    for group_name in sorted(groups_stats):
        teams = list(groups_stats[group_name].values())
        for t in teams:
            t["flag_code"] = flag_map.get(t["team"], "un")
        sorted_groups[group_name] = _sort_group_teams(teams, groups_fixtures.get(group_name, []))

    return sorted_groups


# ── Stats ─────────────────────────────────────────────────────────────────

def get_pool_score_timeline(pool_id, max_players=20):
    """
    Return data for the pool stats chart.

    Result dict:
      fixtures  — list of {id, label, date_label} in kick_off order
      players   — top max_players by final score, each with a `cumulative`
                  list (one value per fixture, starting with 0)
    Only scored fixtures are included.
    """
    from datetime import datetime as _dt
    conn = get_db()

    fix_rows = conn.execute("""
        SELECT DISTINCT f.id, f.kick_off, f.home_team, f.away_team, f.stage
        FROM score_log sl
        JOIN picks p ON p.id = sl.pick_id
        JOIN fixtures f ON f.id = p.fixture_id
        WHERE sl.pool_id = ?
        ORDER BY f.kick_off, f.id
    """, (pool_id,)).fetchall()

    score_rows = conn.execute("""
        SELECT sl.user_id, p.fixture_id, sl.points_awarded, u.display_name
        FROM score_log sl
        JOIN picks p ON p.id = sl.pick_id
        JOIN users u ON u.id = sl.user_id
        WHERE sl.pool_id = ?
    """, (pool_id,)).fetchall()

    conn.close()

    if not fix_rows:
        return {"fixtures": [], "players": []}

    fixture_ids = [r["id"] for r in fix_rows]

    fixtures_out = [{"id": None, "label": "Start", "date_label": ""}]
    for r in fix_rows:
        ko = r["kick_off"] or ""
        try:
            dt = _dt.fromisoformat(ko[:10])
            date_label = f"{dt.day} {dt.strftime('%b')}"
        except Exception:
            date_label = ko[:10]
        ha = (r["home_team"] or "")[:3].upper()
        aa = (r["away_team"] or "")[:3].upper()
        fixtures_out.append({
            "id": r["id"],
            "label": f"{ha} v {aa}",
            "date_label": date_label,
            "stage": r["stage"] or "",
        })

    user_pts = {}
    user_names = {}
    for row in score_rows:
        uid = row["user_id"]
        user_names[uid] = row["display_name"]
        user_pts.setdefault(uid, {})[row["fixture_id"]] = row["points_awarded"]

    players_out = []
    for uid, pts_map in user_pts.items():
        cumulative = [0]
        running = 0
        for fid in fixture_ids:
            running += pts_map.get(fid, 0)
            cumulative.append(running)
        players_out.append({
            "user_id": uid,
            "display_name": user_names[uid],
            "cumulative": cumulative,
            "final_score": running,
        })

    players_out.sort(key=lambda p: p["final_score"], reverse=True)
    return {
        "fixtures": fixtures_out,
        "players": players_out[:max_players],
        "total_players": len(players_out),
    }


# ── Scoring job (kept for backward compat; new code uses process_fixture_result) ──

def calculate_scores_for_fixture(fixture_id):
    """
    Score all picks for a completed fixture via the legacy score_log table.
    Still works for pre-economy data; new fixtures are handled by
    process_fixture_result() which updates pool_members.balance instead.
    Idempotent via the UNIQUE(pick_id) constraint on score_log.
    Returns: number of picks newly scored.
    """
    import uuid as _uuid
    conn = get_db()

    fixture = conn.execute(
        "SELECT result, stage FROM fixtures WHERE id=?", (fixture_id,)
    ).fetchone()
    if not fixture or not fixture["result"]:
        conn.close()
        return 0

    actual = fixture["result"]
    stage  = fixture["stage"] or ''

    picks_for_fixture = conn.execute(
        "SELECT * FROM picks WHERE fixture_id=?", (fixture_id,)
    ).fetchall()

    scored = 0
    for pick in picks_for_fixture:
        config = get_scoring_config(pick["pool_id"], stage)
        points = config["points_win"] if pick["predicted_result"] == actual else config["points_loss"]

        try:
            conn.execute("""
                INSERT INTO score_log (id, user_id, pool_id, pick_id, points_awarded)
                VALUES (?,?,?,?,?)
            """, (str(_uuid.uuid4()), pick["user_id"], pick["pool_id"], pick["id"], points))
            scored += 1
        except _IntegrityError:
            pass

    conn.commit()
    conn.close()
    return scored
