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

    # One row per (user, pool) the first time they open the group-stage
    # Wrapped retrospective. Used to suppress the auto-popup on second
    # and subsequent pool-page loads.
    c.execute("""
        CREATE TABLE IF NOT EXISTS wrapped_views (
            user_id    TEXT NOT NULL REFERENCES users(id),
            pool_id    TEXT NOT NULL REFERENCES pools(id),
            viewed_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, pool_id)
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

    # Records who has paid to reveal the H/D/A vote distribution for a fixture
    # (the "Spy the Field" feature). UNIQUE prevents double-charging.
    c.execute("""
        CREATE TABLE IF NOT EXISTS aggregate_spy_log (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL REFERENCES users(id),
            pool_id     TEXT NOT NULL REFERENCES pools(id),
            fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
            cost        REAL NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, pool_id, fixture_id)
        )
    """)

    # Live-match chat — one row per posted message, soft-deleted on
    # fixture FINISH so history is preserved but not surfaced again.
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          TEXT PRIMARY KEY,
            pool_id     TEXT NOT NULL REFERENCES pools(id),
            fixture_id  TEXT NOT NULL REFERENCES fixtures(id),
            user_id     TEXT NOT NULL REFERENCES users(id),
            body        TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            deleted_at  TEXT
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_pool_fix_created
            ON chat_messages(pool_id, fixture_id, created_at)
    """)
    # Per-message thumbs up/down votes. One row per (message, voter); the
    # only effect is to scale the message font size in the UI. Counts are
    # never shown to users.
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_votes (
            message_id  TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            vote        INTEGER NOT NULL,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, user_id)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_votes_msg
            ON chat_votes(message_id)
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
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS aggregate_spy_cost REAL DEFAULT 2",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS ko_spy_pct REAL DEFAULT 0.10",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS ko_spy_cap REAL DEFAULT 20",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS team_name TEXT",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS live_home_score INTEGER",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS live_away_score INTEGER",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS live_status TEXT",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS live_updated_at TEXT",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS live_minute INTEGER",
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS city TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS chat_banned INTEGER DEFAULT 0",
            "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS posted_minute INTEGER",
            # Set by sim/fetch_odds.py the first cron run that finds a
            # fixture within LOCK_HOURS of kickoff. After that, the row's
            # home_odds / away_odds are frozen and the admin form refuses
            # writes (without an override).
            "ALTER TABLE fixtures ADD COLUMN IF NOT EXISTS odds_locked_at TEXT",
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
            ("scoring_config", "aggregate_spy_cost REAL DEFAULT 2"),
            ("scoring_config", "ko_spy_pct REAL DEFAULT 0.10"),
            ("scoring_config", "ko_spy_cap REAL DEFAULT 20"),
            ("users",          "team_name TEXT"),
            ("fixtures",       "live_home_score INTEGER"),
            ("fixtures",       "live_away_score INTEGER"),
            ("fixtures",       "live_status TEXT"),
            ("fixtures",       "live_updated_at TEXT"),
            ("fixtures",       "live_minute INTEGER"),
            ("fixtures",       "city TEXT"),
            ("users",          "chat_banned INTEGER DEFAULT 0"),
            ("chat_messages",  "posted_minute INTEGER"),
            ("fixtures",       "odds_locked_at TEXT"),
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


def get_open_ko_bet_total(user_id, pool_id):
    """Sum of bet_amount across the user's KO picks on fixtures that
    haven't been settled yet. Drives the BET circle on the navbar."""
    if not user_id or not pool_id:
        return 0.0
    placeholders = ",".join("?" * len(_KNOCKOUT_STAGES))
    conn = get_db()
    row = conn.execute(
        f"""SELECT COALESCE(SUM(p.bet_amount), 0.0) AS total
              FROM picks p
              JOIN fixtures f ON f.id = p.fixture_id
             WHERE p.user_id = ?
               AND p.pool_id = ?
               AND p.bet_amount IS NOT NULL
               AND (f.result IS NULL OR f.result = '')
               AND f.stage IN ({placeholders})""",
        (user_id, pool_id, *_KNOCKOUT_STAGES)
    ).fetchone()
    conn.close()
    return float(row["total"] or 0)


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


def update_user_profile(user_id, display_name, team_name):
    """
    Update a user's real first name + optional team name. team_name=None or
    an empty/whitespace-only string is stored as NULL so the leaderboard
    falls back to display_name.
    """
    display_name = (display_name or "").strip()
    if team_name is not None:
        team_name = team_name.strip() or None
    conn = get_db()
    conn.execute(
        "UPDATE users SET display_name=?, team_name=? WHERE id=?",
        (display_name, team_name, user_id)
    )
    conn.commit()
    conn.close()


def update_user_password(user_id, new_password_hash):
    """Rotate a user's password hash. Caller is responsible for hashing."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (new_password_hash, user_id)
    )
    conn.commit()
    conn.close()


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
    Return all members of a pool ranked by TOTAL worth (free balance plus
    any open KO bets that haven't settled yet), highest first. Each row:
    user_id, display_name, team_name, email, balance (= total), free_balance,
    open_bets, joined_at.

    'balance' is the field other code reads — kept as the total so existing
    callers (chart, navbar rank, etc.) see consistent numbers without
    accidentally double-counting a wagered ante.
    """
    placeholders = ",".join("?" * len(_KNOCKOUT_STAGES))
    conn = get_db()
    rows = conn.execute(f"""
        SELECT u.id AS user_id, u.display_name, u.team_name, u.email,
               COALESCE(pm.balance, 100.0) AS free_balance,
               COALESCE((
                 SELECT SUM(p.bet_amount)
                   FROM picks p
                   JOIN fixtures f ON f.id = p.fixture_id
                  WHERE p.user_id = u.id
                    AND p.pool_id = pm.pool_id
                    AND p.bet_amount IS NOT NULL
                    AND (f.result IS NULL OR f.result = '')
                    AND f.stage IN ({placeholders})
               ), 0) AS open_bets,
               (COALESCE(pm.balance, 100.0) +
                COALESCE((
                  SELECT SUM(p.bet_amount)
                    FROM picks p
                    JOIN fixtures f ON f.id = p.fixture_id
                   WHERE p.user_id = u.id
                     AND p.pool_id = pm.pool_id
                     AND p.bet_amount IS NOT NULL
                     AND (f.result IS NULL OR f.result = '')
                     AND f.stage IN ({placeholders})
                ), 0)) AS balance,
               pm.joined_at
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.pool_id = ?
        ORDER BY balance DESC, pm.joined_at ASC
    """, (*_KNOCKOUT_STAGES, *_KNOCKOUT_STAGES, pool_id)).fetchall()
    conn.close()
    return rows


# ── Fixture helpers ────────────────────────────────────────────────────────

def upsert_fixture(fixture):
    """
    Insert or update a fixture row.

    fixture: a dict with keys: id, home_team, away_team, home_flag_code,
             away_flag_code, kick_off, stage

    NEVER overwrites a real team name with 'TBD'/empty. football-data has
    been observed to "forget" KO team assignments during late group-stage
    flux (returning None for matches that previously had real teams). The
    upsert only ratchets forward — TBD→Brazil is allowed, Brazil→TBD is
    not. Same protection on flag codes (no real team → 'un').
    """
    conn = get_db()
    conn.execute("""
        INSERT INTO fixtures (id, home_team, away_team, home_flag_code, away_flag_code, kick_off, stage)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            home_team      = CASE
                WHEN excluded.home_team IS NULL
                  OR excluded.home_team = ''
                  OR excluded.home_team = 'TBD'
                THEN fixtures.home_team
                ELSE excluded.home_team
            END,
            away_team      = CASE
                WHEN excluded.away_team IS NULL
                  OR excluded.away_team = ''
                  OR excluded.away_team = 'TBD'
                THEN fixtures.away_team
                ELSE excluded.away_team
            END,
            home_flag_code = CASE
                WHEN excluded.home_flag_code IS NULL
                  OR excluded.home_flag_code = ''
                  OR excluded.home_flag_code = 'un'
                THEN fixtures.home_flag_code
                ELSE excluded.home_flag_code
            END,
            away_flag_code = CASE
                WHEN excluded.away_flag_code IS NULL
                  OR excluded.away_flag_code = ''
                  OR excluded.away_flag_code = 'un'
                THEN fixtures.away_flag_code
                ELSE excluded.away_flag_code
            END,
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


def update_live_score(fixture_id, home_score, away_score, status, updated_at,
                       minute=None, city=None):
    """
    Stash a running live score for a fixture (separate from the final
    home_score/away_score that drive payouts). Called by the auto-score
    cron when a match status is IN_PLAY / PAUSED. When the match later
    goes FINISHED, clear_live_score() wipes the live_* columns so the
    pill falls back to rendering the final score.

    minute / city are optional — only updated when provided so a
    follow-up call from a different source (football-data has no
    minute or city) doesn't overwrite the richer data.
    """
    conn = get_db()
    if minute is not None and city is not None:
        conn.execute(
            "UPDATE fixtures SET live_home_score=?, live_away_score=?, "
            "live_status=?, live_updated_at=?, live_minute=?, city=? WHERE id=?",
            (home_score, away_score, status, updated_at, minute, city, fixture_id)
        )
    elif minute is not None:
        conn.execute(
            "UPDATE fixtures SET live_home_score=?, live_away_score=?, "
            "live_status=?, live_updated_at=?, live_minute=? WHERE id=?",
            (home_score, away_score, status, updated_at, minute, fixture_id)
        )
    elif city is not None:
        conn.execute(
            "UPDATE fixtures SET live_home_score=?, live_away_score=?, "
            "live_status=?, live_updated_at=?, city=? WHERE id=?",
            (home_score, away_score, status, updated_at, city, fixture_id)
        )
    else:
        conn.execute(
            "UPDATE fixtures SET live_home_score=?, live_away_score=?, "
            "live_status=?, live_updated_at=? WHERE id=?",
            (home_score, away_score, status, updated_at, fixture_id)
        )
    conn.commit()
    conn.close()


def clear_live_score(fixture_id):
    """Clear running live data when a fixture transitions to FINISHED.
    Preserves city (it's the venue, not a transient state)."""
    conn = get_db()
    conn.execute(
        "UPDATE fixtures SET live_home_score=NULL, live_away_score=NULL, "
        "live_status=NULL, live_updated_at=NULL, live_minute=NULL WHERE id=?",
        (fixture_id,)
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
    "aggregate_spy_cost":              2.0,
    "ko_spy_pct":                      0.10,
    "ko_spy_cap":                      20.0,
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


def get_user_current_streak(user_id, pool_id):
    """
    Walk this user's picks on completed fixtures in chronological order
    (kick_off DESC) and count the run of correct predictions starting from
    the most recent. Returns 0 if the most recent pick was wrong.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT p.predicted_result, f.result
        FROM picks p JOIN fixtures f ON f.id = p.fixture_id
        WHERE p.user_id=? AND p.pool_id=?
          AND f.result IS NOT NULL AND f.result <> ''
        ORDER BY f.kick_off DESC
    """, (user_id, pool_id)).fetchall()
    conn.close()
    streak = 0
    for r in rows:
        if r["predicted_result"] == r["result"]:
            streak += 1
        else:
            break
    return streak


def get_user_rarest_pick(user_id, pool_id):
    """
    Find this user's rarest CORRECT pick — i.e. a winning prediction
    where the smallest share of the pool also picked the same side.
    Returns {fixture, pick, was_correct=True, field_pct} or None if the
    user has no correct picks on completed fixtures yet.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            f.id AS fixture_id,
            f.home_team, f.away_team,
            f.home_flag_code, f.away_flag_code,
            f.home_score, f.away_score, f.result,
            p.predicted_result AS my_pick,
            (
                SELECT CAST(SUM(CASE WHEN p2.predicted_result=p.predicted_result THEN 1 ELSE 0 END) AS REAL)
                       * 100.0 /
                       NULLIF(COUNT(*), 0)
                FROM picks p2
                WHERE p2.pool_id=? AND p2.fixture_id=p.fixture_id
            ) AS field_pct
        FROM picks p JOIN fixtures f ON f.id = p.fixture_id
        WHERE p.user_id=? AND p.pool_id=?
          AND f.result IS NOT NULL AND f.result <> ''
          AND p.predicted_result = f.result    -- correct picks only
        ORDER BY field_pct ASC, f.kick_off DESC
        LIMIT 1
    """, (pool_id, user_id, pool_id)).fetchone()
    conn.close()
    if not row_present(rows):
        return None
    r = rows
    return {
        "fixture_id": r["fixture_id"],
        "home_team":  r["home_team"],
        "away_team":  r["away_team"],
        "home_flag":  r["home_flag_code"] or "un",
        "away_flag":  r["away_flag_code"] or "un",
        "home_score": r["home_score"],
        "away_score": r["away_score"],
        "result":     r["result"],
        "my_pick":    r["my_pick"],
        "was_correct": r["my_pick"] == r["result"],
        "field_pct":  float(r["field_pct"] or 0),
    }


def row_present(row):
    """Small null-safe predicate for sqlite/Postgres rows."""
    return row is not None


def get_user_riskiest_bet(user_id, pool_id):
    """Return the user's biggest single KO ante in this pool — settled
    matches only. Open bets are excluded so a public leaderboard can't
    leak who's wagered what on a fixture before it locks (otherwise this
    would amount to free spy intel).

    Returns {amount, home_team, away_team, home_flag, away_flag, pick,
    home_score, away_score, result, mult, payout} or None when the user
    has no settled KO bets yet.
    """
    placeholders = ",".join("?" * len(_KNOCKOUT_STAGES))
    conn = get_db()
    row = conn.execute(
        f"""SELECT p.bet_amount AS amount,
                   f.home_team, f.away_team, f.home_flag_code, f.away_flag_code,
                   p.predicted_result AS pick,
                   f.home_score, f.away_score, f.result,
                   f.home_odds, f.away_odds
              FROM picks p
              JOIN fixtures f ON f.id = p.fixture_id
             WHERE p.user_id = ?
               AND p.pool_id = ?
               AND p.bet_amount IS NOT NULL
               AND p.bet_amount > 0
               AND f.stage IN ({placeholders})
               AND f.result IS NOT NULL AND f.result <> ''
             ORDER BY p.bet_amount DESC
             LIMIT 1""",
        (user_id, pool_id, *_KNOCKOUT_STAGES),
    ).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    mult = (r["home_odds"] if r["pick"] == "H" else
            r["away_odds"] if r["pick"] == "A" else None)
    won  = r["result"] and r["pick"] == r["result"]
    payout = float(r["amount"]) * mult if (won and mult) else (0.0 if r["result"] else None)
    return {
        "amount":     float(r["amount"]),
        "home_team":  r["home_team"],
        "away_team":  r["away_team"],
        "home_flag":  r["home_flag_code"] or "un",
        "away_flag":  r["away_flag_code"] or "un",
        "pick":       r["pick"],
        "home_score": r["home_score"],
        "away_score": r["away_score"],
        "result":     r["result"],
        "mult":       float(mult) if mult else None,
        "payout":     payout,
    }


def get_user_largest_win(user_id, pool_id):
    """Return the user's biggest single payout in this pool — KO winners
    only since group-stage wins are flat. Used as a 'largest win' stat
    in the leaderboard detailed view.

    Returns {amount, bet_amount, home_team, away_team, home_flag,
    away_flag, pick, home_score, away_score, mult} or None when the user
    has no winning KO bets.
    """
    placeholders = ",".join("?" * len(_KNOCKOUT_STAGES))
    conn = get_db()
    row = conn.execute(
        f"""SELECT t.amount,
                   f.home_team, f.away_team, f.home_flag_code, f.away_flag_code,
                   p.predicted_result AS pick, p.bet_amount,
                   f.home_score, f.away_score, f.home_odds, f.away_odds
              FROM transactions t
              JOIN fixtures f ON f.id = t.fixture_id
              JOIN picks p     ON p.fixture_id = t.fixture_id
                              AND p.user_id    = t.user_id
                              AND p.pool_id    = t.pool_id
             WHERE t.user_id = ?
               AND t.pool_id = ?
               AND t.type    = 'payout'
               AND t.amount  > 0
               AND f.stage IN ({placeholders})
             ORDER BY t.amount DESC
             LIMIT 1""",
        (user_id, pool_id, *_KNOCKOUT_STAGES),
    ).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    mult = (r["home_odds"] if r["pick"] == "H" else
            r["away_odds"] if r["pick"] == "A" else None)
    return {
        "amount":     float(r["amount"]),
        "bet_amount": float(r["bet_amount"] or 0),
        "home_team":  r["home_team"],
        "away_team":  r["away_team"],
        "home_flag":  r["home_flag_code"] or "un",
        "away_flag":  r["away_flag_code"] or "un",
        "pick":       r["pick"],
        "home_score": r["home_score"],
        "away_score": r["away_score"],
        "mult":       float(mult) if mult else None,
    }


def get_user_pick_accuracy(user_id, pool_id):
    """
    Return {"correct": int, "total": int} for a user's picks on completed
    fixtures. "Correct" means the predicted_result matches the fixture
    result. Used by the pool page's top stats tile ("4/14 picks").
    """
    conn = get_db()
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN p.predicted_result = f.result THEN 1 ELSE 0 END) AS correct, "
        "  COUNT(*) AS total "
        "FROM picks p JOIN fixtures f ON f.id = p.fixture_id "
        "WHERE p.user_id=? AND p.pool_id=? "
        "  AND f.result IS NOT NULL AND f.result <> ''",
        (user_id, pool_id)
    ).fetchone()
    conn.close()
    return {
        "correct": int(row["correct"] or 0) if row else 0,
        "total":   int(row["total"]   or 0) if row else 0,
    }


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


def write_adjustment_transaction(user_id, pool_id, amount, description=""):
    """Apply a signed delta to a member's balance + record an 'adjustment'
    transaction so the timeline (and the stats chart) stays consistent."""
    import uuid as _uuid
    conn = get_db()
    _update_balance(conn, user_id, pool_id, amount)
    conn.execute(
        "INSERT INTO transactions (id, user_id, pool_id, fixture_id, type, amount, description) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(_uuid.uuid4()), user_id, pool_id, None, "adjustment", amount, description)
    )
    conn.commit()
    conn.close()


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


def get_aggregate_spy_fixture_set(user_id, pool_id):
    """Set of fixture_ids the user has bought aggregate spy on for this pool."""
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id FROM aggregate_spy_log WHERE user_id=? AND pool_id=?",
        (user_id, pool_id)
    ).fetchall()
    conn.close()
    return frozenset(r["fixture_id"] for r in rows)


def has_bought_aggregate_spy(user_id, pool_id, fixture_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM aggregate_spy_log WHERE user_id=? AND pool_id=? AND fixture_id=?",
        (user_id, pool_id, fixture_id)
    ).fetchone()
    conn.close()
    return row is not None


def record_aggregate_spy(spy_id, tx_id, user_id, pool_id, fixture_id, cost):
    """
    Idempotently record an aggregate-spy purchase. Returns True if it was a
    NEW purchase (charged), False if the user had already bought it (no charge).
    """
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM aggregate_spy_log WHERE user_id=? AND pool_id=? AND fixture_id=?",
        (user_id, pool_id, fixture_id)
    ).fetchone()
    if existing:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO aggregate_spy_log (id, user_id, pool_id, fixture_id, cost) VALUES (?,?,?,?,?)",
        (spy_id, user_id, pool_id, fixture_id, cost)
    )
    _write_transaction(conn, tx_id, user_id, pool_id, fixture_id, "spy", -cost,
                       "Field spy purchase")
    _update_balance(conn, user_id, pool_id, -cost)
    conn.commit()
    conn.close()
    return True


def get_fixture_pot(pool_id, fixture_id):
    """
    Sum of bet_amount across all picks on this fixture in this pool. Used to
    compute dynamic field-spy pricing on knockout fixtures.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(SUM(bet_amount), 0) AS pot FROM picks WHERE pool_id=? AND fixture_id=?",
        (pool_id, fixture_id)
    ).fetchone()
    conn.close()
    return float(row["pot"] or 0)


def get_fixture_pots_for_pool(pool_id):
    """
    Batched variant of get_fixture_pot: returns {fixture_id: pot_float} for every
    fixture this pool has any picks on. Used by pool_page to avoid an N+1 loop
    when computing dynamic field-spy pricing for ~100 fixtures.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id, COALESCE(SUM(bet_amount), 0) AS pot "
        "FROM picks WHERE pool_id=? GROUP BY fixture_id",
        (pool_id,)
    ).fetchall()
    conn.close()
    return {r["fixture_id"]: float(r["pot"] or 0) for r in rows}


def get_fixture_pick_totals_for_pool(pool_id):
    """
    Batched variant of get_fixture_pick_totals: returns {fixture_id: totals_dict}
    for every fixture with at least one pick in this pool. Each totals_dict has
    the same shape as get_fixture_pick_totals (H/D/A counts + wagered, no_pick,
    total_members). Caller decides per-fixture whether knockout wagered sums
    matter.

    Eliminates the pool_page N+1 that opened one Postgres connection per
    completed/locked fixture.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id, predicted_result, "
        "       COUNT(*) AS n, COALESCE(SUM(bet_amount), 0) AS sum_bet "
        "FROM picks WHERE pool_id=? "
        "GROUP BY fixture_id, predicted_result",
        (pool_id,)
    ).fetchall()
    members_row = conn.execute(
        "SELECT COUNT(*) AS n FROM pool_members WHERE pool_id=?", (pool_id,)
    ).fetchone()
    conn.close()
    total_members = members_row["n"] or 0

    by_fid = {}
    for r in rows:
        fid  = r["fixture_id"]
        side = r["predicted_result"]
        if fid not in by_fid:
            by_fid[fid] = {
                "H": {"count": 0, "wagered": 0.0},
                "D": {"count": 0, "wagered": None},
                "A": {"count": 0, "wagered": 0.0},
                "picked": 0,
                "total_members": total_members,
            }
        if side in by_fid[fid]:
            by_fid[fid][side]["count"] = r["n"]
            if side in ("H", "A"):
                by_fid[fid][side]["wagered"] = float(r["sum_bet"] or 0)
            by_fid[fid]["picked"] += r["n"]
    # Materialise no_pick after aggregation
    for fid, t in by_fid.items():
        t["no_pick"] = max(0, total_members - t.pop("picked"))
    return by_fid


def get_pick_counts_for_pool(pool_id):
    """
    Return {fixture_id: int} — how many picks have been submitted on each fixture
    by anyone in this pool. Just a count; no per-side breakdown (that's a spy).
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT fixture_id, COUNT(*) AS n FROM picks WHERE pool_id=? GROUP BY fixture_id",
        (pool_id,)
    ).fetchall()
    conn.close()
    return {r["fixture_id"]: r["n"] for r in rows}


def get_fixture_pick_totals(pool_id, fixture_id, knockout=False):
    """
    Vote distribution for one fixture in one pool. Counts each predicted
    result and (for KO) sums bet_amount per side.

    Returns:
      {
        "H": {"count": int, "wagered": float|None},
        "D": {"count": int, "wagered": None},
        "A": {"count": int, "wagered": float|None},
        "no_pick": int,
        "total_members": int
      }
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT predicted_result, "
        "       COUNT(*) AS n, "
        "       COALESCE(SUM(bet_amount), 0) AS sum_bet "
        "FROM picks WHERE pool_id=? AND fixture_id=? "
        "GROUP BY predicted_result",
        (pool_id, fixture_id)
    ).fetchall()
    members = conn.execute(
        "SELECT COUNT(*) AS n FROM pool_members WHERE pool_id=?", (pool_id,)
    ).fetchone()
    conn.close()

    totals = {"H": {"count": 0, "wagered": (0.0 if knockout else None)},
              "D": {"count": 0, "wagered": None},
              "A": {"count": 0, "wagered": (0.0 if knockout else None)}}
    picked = 0
    for r in rows:
        side = r["predicted_result"]
        if side in totals:
            totals[side]["count"] = r["n"]
            if knockout and side in ("H", "A"):
                totals[side]["wagered"] = float(r["sum_bet"] or 0)
            picked += r["n"]
    return {
        **totals,
        "no_pick": max(0, (members["n"] or 0) - picked),
        "total_members": (members["n"] or 0),
    }


def get_top_n_player_emails(pool_id, n=5, tie_cap=2):
    """
    Return a set of emails for the top N players in a pool by balance.
    Within any tied bucket, at most `tie_cap` players are included. This
    keeps the lock-time "top picks" list compact even when several players
    are clustered at the same balance.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT u.email, COALESCE(pm.balance, 100.0) AS bal "
        "FROM pool_members pm JOIN users u ON u.id = pm.user_id "
        "WHERE pm.pool_id = ? "
        "ORDER BY bal DESC, pm.joined_at ASC",
        (pool_id,)
    ).fetchall()
    conn.close()

    emails = []
    i = 0
    while i < len(rows) and len(emails) < n:
        # Walk to the end of the current tied group (≈ same balance)
        j = i + 1
        current_bal = float(rows[i]["bal"] or 0)
        while j < len(rows) and abs(float(rows[j]["bal"] or 0) - current_bal) < 0.005:
            j += 1
        # Take up to tie_cap from this group, bounded by remaining slots
        take = min(tie_cap, j - i, n - len(emails))
        for k in range(take):
            emails.append(rows[i + k]["email"])
        i = j
    return frozenset(emails)


def get_member_rank(user_id, pool_id):
    """
    Return (rank, total_members) for the user in this pool's leaderboard,
    sorted by TOTAL worth (free balance + open KO bets) DESC. Rank is
    1-based. Ties resolve by joined_at.
    """
    placeholders = ",".join("?" * len(_KNOCKOUT_STAGES))
    conn = get_db()
    rows = conn.execute(f"""
        SELECT pm.user_id,
               COALESCE(pm.balance, 100.0) +
               COALESCE((
                 SELECT SUM(p.bet_amount)
                   FROM picks p
                   JOIN fixtures f ON f.id = p.fixture_id
                  WHERE p.user_id = pm.user_id
                    AND p.pool_id = pm.pool_id
                    AND p.bet_amount IS NOT NULL
                    AND (f.result IS NULL OR f.result = '')
                    AND f.stage IN ({placeholders})
               ), 0) AS total_worth
          FROM pool_members pm
         WHERE pm.pool_id = ?
         ORDER BY total_worth DESC, pm.joined_at ASC
    """, (*_KNOCKOUT_STAGES, pool_id)).fetchall()
    conn.close()
    total = len(rows)
    for i, r in enumerate(rows):
        if r["user_id"] == user_id:
            return (i + 1, total)
    return (None, total)


def get_pool_members_for_spy_list(buyer_id, pool_id, fixture_id):
    """
    All other members of this pool plus whether the buyer has already spied
    on them for this fixture. Sorted by balance DESC. Used to populate the
    'Spy on Someone' modal.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT u.id, u.display_name, u.email, "
        "       COALESCE(pm.balance, 100.0) AS balance, "
        "       EXISTS(SELECT 1 FROM spy_log s "
        "              WHERE s.buyer_id=? AND s.target_id=u.id "
        "                AND s.pool_id=? AND s.fixture_id=?) AS already_spied "
        "FROM pool_members pm "
        "JOIN users u ON u.id = pm.user_id "
        "WHERE pm.pool_id=? AND pm.user_id != ? "
        "ORDER BY balance DESC, u.display_name ASC",
        (buyer_id, pool_id, fixture_id, pool_id, buyer_id)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def get_spy_activity_by_day(days=14):
    """
    Daily roll-up of spy activity across BOTH spy_log (per-target) and
    aggregate_spy_log (whole-fixture). Returns most-recent first.

    Returns: [
        {"day": "2026-06-27",
         "target_spies": int, "target_spend": float,
         "field_spies":  int, "field_spend":  float,
         "total_spies":  int, "total_spend":  float},
        ...
    ]
    Always returns `days` rows, padded with zeros where there was no
    activity, so the ticker shows a stable timeline.
    """
    conn = get_db()
    # Both tables use a TEXT created_at (ISO-ish "YYYY-MM-DD HH:MM:SS"
    # from CURRENT_TIMESTAMP) so substr(...,1,10) gives a portable day
    # bucket on both SQLite and Postgres.
    target_rows = conn.execute("""
        SELECT substr(created_at, 1, 10) AS day,
               COUNT(*) AS n, COALESCE(SUM(cost), 0) AS spend
        FROM spy_log
        GROUP BY substr(created_at, 1, 10)
    """).fetchall()
    field_rows = conn.execute("""
        SELECT substr(created_at, 1, 10) AS day,
               COUNT(*) AS n, COALESCE(SUM(cost), 0) AS spend
        FROM aggregate_spy_log
        GROUP BY substr(created_at, 1, 10)
    """).fetchall()
    conn.close()

    by_day = {}
    for r in target_rows:
        d = dict(r)
        by_day.setdefault(d["day"], {"target_spies": 0, "target_spend": 0.0,
                                      "field_spies": 0, "field_spend": 0.0})
        by_day[d["day"]]["target_spies"] = int(d["n"])
        by_day[d["day"]]["target_spend"] = float(d["spend"])
    for r in field_rows:
        d = dict(r)
        by_day.setdefault(d["day"], {"target_spies": 0, "target_spend": 0.0,
                                      "field_spies": 0, "field_spend": 0.0})
        by_day[d["day"]]["field_spies"] = int(d["n"])
        by_day[d["day"]]["field_spend"] = float(d["spend"])

    # Pad with the last `days` calendar days so the timeline is stable
    # even on quiet days.
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        row = by_day.get(d, {"target_spies": 0, "target_spend": 0.0,
                              "field_spies": 0, "field_spend": 0.0})
        out.append({
            "day": d,
            **row,
            "total_spies": row["target_spies"] + row["field_spies"],
            "total_spend": row["target_spend"] + row["field_spend"],
        })
    return out


def is_group_stage_complete(pool_id):
    """
    True once every group-stage fixture has a result. Used to gate the
    Wrapped retrospective: nothing to wrap until groups are done.
    """
    conn = get_db()
    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) AS done
        FROM fixtures
        WHERE stage LIKE 'Group %'
    """).fetchone()
    conn.close()
    if not row or not row["total"]:
        return False
    return int(row["done"] or 0) >= int(row["total"])


def has_seen_wrapped(user_id, pool_id):
    # Defensive: if the table doesn't exist yet (e.g. boot race before
    # init_db ran on a fresh deploy), treat it as "not seen" rather than
    # propagating a 500 up through the pool page render.
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT 1 FROM wrapped_views WHERE user_id=? AND pool_id=?",
            (user_id, pool_id)
        ).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


def mark_wrapped_seen(user_id, pool_id):
    """Idempotent — does nothing if already marked or the table is missing."""
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO wrapped_views (user_id, pool_id) VALUES (?, ?)",
                (user_id, pool_id)
            )
            conn.commit()
        except _IntegrityError:
            pass  # Already marked; primary key collision is fine.
        conn.close()
    except Exception:
        pass


def get_wrapped_stats(user_id, pool_id):
    """
    Compute every Wrapped slide stat in one round-trip per concept.
    Returns a dict with all numbers; the template just renders them.

    Only counts group-stage fixtures (stage LIKE 'Group %') with a final
    result. Tied breakers documented inline.

    Returned keys:
      net_winnings, correct_picks, total_picks, accuracy_pct,
      best_day {date, payout}, best_group {letter, accuracy, n_correct, n_total},
      worst_group {letter, accuracy, n_correct, n_total},
      similar_bettor {user_id, display_name, team_name, match_count, total_compared},
      neighbours {above, you, below}  (each = {rank, display_name, team_name, balance} or None),
      underestimated_team {team, count}, overestimated_team {team, count}
    """
    conn = get_db()

    # ── 1. Net winnings: sum of payout transactions for group-stage fixtures
    #    (filtered via JOIN to fixtures).
    row = conn.execute("""
        SELECT COALESCE(SUM(t.amount), 0) AS s
        FROM transactions t
        JOIN fixtures f ON f.id = t.fixture_id
        WHERE t.user_id=? AND t.pool_id=? AND t.type='payout'
          AND f.stage LIKE 'Group %'
    """, (user_id, pool_id)).fetchone()
    net_winnings = float(row["s"] or 0.0)

    # ── 2. Correct picks + total picks across group stage.
    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN p.predicted_result = f.result THEN 1 ELSE 0 END) AS correct
        FROM picks p
        JOIN fixtures f ON f.id = p.fixture_id
        WHERE p.user_id=? AND p.pool_id=?
          AND f.stage LIKE 'Group %'
          AND f.result IS NOT NULL
    """, (user_id, pool_id)).fetchone()
    total_picks   = int(row["total"] or 0)
    correct_picks = int(row["correct"] or 0)
    accuracy_pct  = round(100.0 * correct_picks / total_picks, 1) if total_picks else 0.0

    # ── 3. Best day of betting: day with the largest sum of payouts,
    #    grouped by the FIXTURE'S kick_off date (NOT the transaction
    #    created_at -- the scorer often re-runs and stamps everything
    #    with one date, which made the original implementation collapse
    #    every payout into "the day the scorer ran").
    days = conn.execute("""
        SELECT substr(f.kick_off, 1, 10) AS day,
               COALESCE(SUM(t.amount), 0) AS payout
        FROM transactions t
        JOIN fixtures f ON f.id = t.fixture_id
        WHERE t.user_id=? AND t.pool_id=? AND t.type='payout'
          AND f.stage LIKE 'Group %'
          AND f.kick_off IS NOT NULL
        GROUP BY substr(f.kick_off, 1, 10)
        ORDER BY payout DESC, day DESC
        LIMIT 1
    """, (user_id, pool_id)).fetchone()
    best_day = ({"date": days["day"], "payout": float(days["payout"])}
                if days and float(days["payout"]) > 0 else None)

    # Attach every pick the user made on the best day so the slide can
    # render a recap table (including wrong ones). Same row shape as
    # the best/worst-group fixture detail above.
    if best_day:
        rows = conn.execute("""
            SELECT f.kick_off, f.home_team, f.away_team,
                   f.home_score, f.away_score, f.result,
                   p.predicted_result,
                   COALESCE((SELECT t.amount FROM transactions t
                              WHERE t.user_id=? AND t.pool_id=?
                                AND t.fixture_id=f.id AND t.type='payout'),
                            0) AS payout
            FROM fixtures f
            JOIN picks p ON p.fixture_id = f.id
                        AND p.user_id=? AND p.pool_id=?
            WHERE f.stage LIKE 'Group %'
              AND f.result IS NOT NULL
              AND substr(f.kick_off, 1, 10) = ?
            ORDER BY f.kick_off
        """, (user_id, pool_id, user_id, pool_id, best_day["date"])).fetchall()
        fxs = []
        for r in rows:
            d = dict(r)
            d["got_it_right"] = (d.get("predicted_result") == d.get("result"))
            d["payout"]       = float(d.get("payout") or 0)
            fxs.append(d)
        best_day["fixtures"] = fxs

    # ── 4. Per-group accuracy → best + worst.
    #    Tie-break: most picks first (more data), then alphabetic.
    groups = conn.execute("""
        SELECT f.stage AS stage,
               COUNT(*) AS n_total,
               SUM(CASE WHEN p.predicted_result = f.result THEN 1 ELSE 0 END) AS n_correct
        FROM picks p
        JOIN fixtures f ON f.id = p.fixture_id
        WHERE p.user_id=? AND p.pool_id=?
          AND f.stage LIKE 'Group %'
          AND f.result IS NOT NULL
        GROUP BY f.stage
    """, (user_id, pool_id)).fetchall()
    g_rows = []
    for g in groups:
        n_total = int(g["n_total"] or 0)
        n_correct = int(g["n_correct"] or 0)
        if n_total == 0:
            continue
        g_rows.append({
            "letter":    (g["stage"] or "")[6:],  # 'Group A' -> 'A'
            "n_total":   n_total,
            "n_correct": n_correct,
            "accuracy":  round(100.0 * n_correct / n_total, 1),
        })
    best_group  = max(g_rows, key=lambda r: (r["accuracy"],  r["n_total"], -ord(r["letter"][0]))) if g_rows else None
    worst_group = min(g_rows, key=lambda r: (r["accuracy"], -r["n_total"],  ord(r["letter"][0]))) if g_rows else None

    # Attach the fixture detail for the best & worst groups so the slide
    # can render a recap table -- same shape as the Completed table on
    # the pool page (date, time, home, score, away, my pick, payout).
    def _fixture_detail(stage_name):
        rows = conn.execute("""
            SELECT f.kick_off, f.home_team, f.away_team, f.home_flag_code,
                   f.away_flag_code, f.home_score, f.away_score, f.result,
                   p.predicted_result,
                   COALESCE((SELECT t.amount FROM transactions t
                              WHERE t.user_id=? AND t.pool_id=?
                                AND t.fixture_id=f.id AND t.type='payout'),
                            0) AS payout
            FROM fixtures f
            JOIN picks p ON p.fixture_id = f.id
                        AND p.user_id=? AND p.pool_id=?
            WHERE f.stage=? AND f.result IS NOT NULL
            ORDER BY f.kick_off
        """, (user_id, pool_id, user_id, pool_id, stage_name)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["got_it_right"] = (d.get("predicted_result") == d.get("result"))
            d["payout"]       = float(d.get("payout") or 0)
            out.append(d)
        return out

    if best_group:
        best_group["fixtures"]  = _fixture_detail("Group " + best_group["letter"])
    if worst_group and (not best_group or worst_group["letter"] != best_group["letter"]):
        worst_group["fixtures"] = _fixture_detail("Group " + worst_group["letter"])
    elif worst_group:
        worst_group["fixtures"] = best_group["fixtures"]  # same group, share list

    # ── 5. Most similar bettor: max count of matching (fixture, prediction)
    #    tuples vs the current user across the group stage.
    similar = conn.execute("""
        WITH me AS (
            SELECT p.fixture_id, p.predicted_result
            FROM picks p
            JOIN fixtures f ON f.id = p.fixture_id
            WHERE p.user_id=? AND p.pool_id=? AND f.stage LIKE 'Group %'
        )
        SELECT u.id AS user_id, u.display_name,
               u.team_name,
               COUNT(*) AS match_count,
               (SELECT COUNT(*) FROM me) AS total_compared
        FROM picks p
        JOIN me ON me.fixture_id = p.fixture_id AND me.predicted_result = p.predicted_result
        JOIN users u ON u.id = p.user_id
        WHERE p.pool_id=? AND p.user_id <> ?
        GROUP BY u.id, u.display_name, u.team_name
        ORDER BY match_count DESC, u.display_name ASC
        LIMIT 1
    """, (user_id, pool_id, pool_id, user_id)).fetchone()
    similar_bettor = (dict(similar) if similar else None)
    if similar_bettor:
        similar_bettor["match_count"]    = int(similar_bettor["match_count"])
        similar_bettor["total_compared"] = int(similar_bettor["total_compared"])

    # ── 6. Leaderboard neighbours (rank-1, you, rank+1).
    #    Mirrors get_member_rank: total_worth = free balance + open KO
    #    bets, sorted DESC with joined_at as tiebreak. Anything else
    #    drifts away from the rank the user sees everywhere else.
    placeholders = ",".join("?" * len(_KNOCKOUT_STAGES))
    lb = conn.execute(f"""
        SELECT pm.user_id, u.display_name, u.team_name,
               COALESCE(pm.balance, 100.0) +
               COALESCE((
                 SELECT SUM(p.bet_amount)
                   FROM picks p
                   JOIN fixtures f ON f.id = p.fixture_id
                  WHERE p.user_id = pm.user_id
                    AND p.pool_id = pm.pool_id
                    AND p.bet_amount IS NOT NULL
                    AND (f.result IS NULL OR f.result = '')
                    AND f.stage IN ({placeholders})
               ), 0) AS balance
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        WHERE pm.pool_id=?
        ORDER BY balance DESC, pm.joined_at ASC
    """, (*_KNOCKOUT_STAGES, pool_id)).fetchall()
    lb_list = [dict(r) for r in lb]
    my_idx = next((i for i, r in enumerate(lb_list) if r["user_id"] == user_id), None)
    def _row_for(i):
        if i is None or i < 0 or i >= len(lb_list):
            return None
        r = lb_list[i]
        return {"rank": i + 1, "display_name": r["display_name"],
                "team_name": r["team_name"], "balance": float(r["balance"] or 0)}
    neighbours = {
        "above": _row_for(my_idx - 1) if my_idx is not None else None,
        "you":   _row_for(my_idx),
        "below": _row_for(my_idx + 1) if my_idx is not None else None,
    }

    # ── 7. Underestimated team: team won most often when you picked against.
    #    Overestimated team: team lost most often when you picked them.
    #    For each pick + result, attribute one side to each team in the
    #    match and check if user's pick aligned with the winner.
    rows = conn.execute("""
        SELECT f.home_team, f.away_team, f.result, p.predicted_result
        FROM picks p
        JOIN fixtures f ON f.id = p.fixture_id
        WHERE p.user_id=? AND p.pool_id=?
          AND f.stage LIKE 'Group %'
          AND f.result IS NOT NULL
    """, (user_id, pool_id)).fetchall()
    under = {}  # team -> count of times they won when user picked against
    over  = {}  # team -> count of times they lost when user picked them
    for r in rows:
        h, a, res, pred = r["home_team"], r["away_team"], r["result"], r["predicted_result"]
        if not h or not a or h.startswith("TBD") or a.startswith("TBD"):
            continue
        if res == "H":
            # Home won. If user picked A (or D), they underestimated home.
            # Also: if user picked H, they overestimated away.
            if pred in ("A", "D"):
                under[h] = under.get(h, 0) + 1
            if pred == "H":
                over[a]  = over.get(a, 0) + 1
        elif res == "A":
            if pred in ("H", "D"):
                under[a] = under.get(a, 0) + 1
            if pred == "A":
                over[h]  = over.get(h, 0) + 1
        # Draws don't have a "winner" to under/overestimate.
    def _top(counts):
        if not counts:
            return None
        team, count = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        return {"team": team, "count": count}
    underestimated_team = _top(under)
    overestimated_team  = _top(over)

    conn.close()

    return {
        "net_winnings":         net_winnings,
        "correct_picks":        correct_picks,
        "total_picks":          total_picks,
        "accuracy_pct":         accuracy_pct,
        "best_day":             best_day,
        "best_group":           best_group,
        "worst_group":          worst_group,
        "similar_bettor":       similar_bettor,
        "neighbours":           neighbours,
        "underestimated_team":  underestimated_team,
        "overestimated_team":   overestimated_team,
    }


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
    # Soft-delete all chat messages for this fixture so future loads of
    # the live banner (or admin moderation views) skip them. Preserves
    # history in the DB for auditing.
    soft_delete_fixture_chat(fixture_id)
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
    # Pull EVERY group fixture so we can seed the standings with all 4
    # teams in each group, even before any matches have been played.
    # Result-bearing rows then layer on the played-game stats; unplayed
    # rows just contribute team identity (and flag).
    conn = get_db()
    rows = conn.execute("""
        SELECT stage, home_team, away_team, home_flag_code, away_flag_code,
               home_score, away_score, result
        FROM fixtures
        WHERE stage LIKE 'Group %'
        ORDER BY stage, kick_off
    """).fetchall()
    conn.close()

    if not rows:
        return {}

    raw_rows = [dict(r) for r in rows]

    # Build stats per group
    groups_stats = {}   # group → {team → stat_dict}
    groups_fixtures = {}  # group → [completed fixture rows only — used for H2H]
    flag_map = {}

    for r in raw_rows:
        group = r["stage"]
        home, away = r["home_team"], r["away_team"]

        flag_map[home] = r["home_flag_code"] or "un"
        flag_map[away] = r["away_flag_code"] or "un"

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

        # Only completed fixtures contribute to the played-game stats + H2H
        if not r["result"]:
            continue
        groups_fixtures.setdefault(group, []).append(r)

        hs, as_ = (r["home_score"] or 0), (r["away_score"] or 0)
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
    """Legacy alias — calls get_pool_balance_timeline. The stats page now
    plots running balance, not the old points-only score_log accumulation."""
    return get_pool_balance_timeline(pool_id, max_players=max_players)


def _parse_ts(s):
    """Parse a created_at TEXT column to a datetime. SQLite stores
    'YYYY-MM-DD HH:MM:SS'; Postgres stores ISO with optional timezone."""
    from datetime import datetime as _dt, timezone as _tz
    if s is None: return None
    s = str(s).strip()
    if not s: return None
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = _dt.fromisoformat(s)
    except Exception:
        return None
    # Normalise to aware UTC so cross-row comparisons are well-defined
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt


def get_pool_balance_per_match(pool_id, max_players=20):
    """
    Per-match cumulative balance per player — used by the combined Stats
    page so the x-axis can show one tick per scored match (instead of a
    real time axis). Each scored fixture becomes a column; the value at
    column N is the player's running balance after that match resolved.

    fixtures[] is in kick_off order. cumulative[0] is the starting
    balance (synthetic "Start" column); cumulative[i] for i>=1 corresponds
    to fixtures[i-1]. Players also carry a parallel `details[]` list — one
    entry per column — with {pick, result, bet_amount, delta} so the
    chart tooltip can show what the user did on that specific match.
    """
    from collections import defaultdict
    conn = get_db()
    sc = conn.execute(
        "SELECT starting_balance FROM scoring_config ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    start_balance = float((sc and sc["starting_balance"]) or 100)

    # Any fixture that has been settled (has a result) AND at least one
    # pool member made a pick on it. KO losses don't write a payout
    # transaction but they still move total worth, so we can't filter by
    # tx alone — we look at picks JOIN fixtures-with-result.
    fix_rows = conn.execute("""
        SELECT DISTINCT f.id, f.kick_off, f.home_team, f.away_team, f.stage
          FROM picks p
          JOIN fixtures f ON f.id = p.fixture_id
         WHERE p.pool_id = ?
           AND f.result IS NOT NULL AND f.result <> ''
         ORDER BY f.kick_off, f.id
    """, (pool_id,)).fetchall()

    tx_rows = conn.execute("""
        SELECT user_id, fixture_id, amount, type, created_at FROM transactions
        WHERE pool_id = ?
    """, (pool_id,)).fetchall()

    member_rows = conn.execute("""
        SELECT pm.user_id, u.display_name, u.team_name, u.email
        FROM pool_members pm JOIN users u ON u.id = pm.user_id
        WHERE pm.pool_id = ?
    """, (pool_id,)).fetchall()

    if not fix_rows or not member_rows:
        conn.close()
        return {"fixtures": [{"id": None, "label": "Start"}],
                "players": [], "total_players": len(member_rows),
                "starting_balance": start_balance}

    fix_id_to_col = {r["id"]: i + 1 for i, r in enumerate(fix_rows)}
    n_cols = len(fix_rows) + 1

    # Pull each fixture's final score upfront so the chart's x-axis can
    # render "1-0" under each match number.
    _ph = ",".join("?" * len(fix_rows))
    _scores_by_fid = {}
    for r in conn.execute(
        f"SELECT id, home_score, away_score FROM fixtures WHERE id IN ({_ph})",
        [r["id"] for r in fix_rows]
    ).fetchall():
        _scores_by_fid[r["id"]] = (r["home_score"], r["away_score"])

    # Derive a per-fixture "round" label for the chart's round-divider lines.
    # Group fixtures get MD1/MD2/MD3 (2 games per matchday, sorted by kick_off
    # within each group). KO stages map to short labels.
    _SHORT_ROUND = {
        "Round of 32":    "R32",
        "Round of 16":    "R16",
        "Quarter-Finals": "QF",
        "Semi-Finals":    "SF",
        "Third Place":    "3rd",
        "Final":          "Final",
    }
    _group_buckets = defaultdict(list)
    round_by_fid = {}
    for r in fix_rows:
        stage = r["stage"] or ""
        if stage.startswith("Group "):
            _group_buckets[stage].append(r)
        else:
            round_by_fid[r["id"]] = _SHORT_ROUND.get(stage, stage)
    for fxs in _group_buckets.values():
        fxs.sort(key=lambda f: f["kick_off"] or "")
        for i, fix in enumerate(fxs):
            round_by_fid[fix["id"]] = f"MD{i // 2 + 1}"

    fixtures_out = [{"id": None, "label": "Start", "date_label": "", "stage": "",
                     "round": "", "home_score": None, "away_score": None}]
    for r in fix_rows:
        ko = r["kick_off"] or ""
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(ko[:10])
            date_label = f"{dt.day} {dt.strftime('%b')}"
        except Exception:
            date_label = ko[:10]
        ha = (r["home_team"] or "")[:3].upper()
        aa = (r["away_team"] or "")[:3].upper()
        hs, as_ = _scores_by_fid.get(r["id"], (None, None))
        fixtures_out.append({
            "id":         r["id"],
            "label":      f"{ha} v {aa}",
            "date_label": date_label,
            "stage":      r["stage"] or "",
            "round":      round_by_fid.get(r["id"], ""),
            "home_score": hs,
            "away_score": as_,
        })

    # Bucket per-user deltas. The chart shows TOTAL WORTH (free balance +
    # open bets) — total only changes at settlement, never at bet-time —
    # so we skip the bet+adjustment pair on KO ante moves and subtract
    # the wagered amount from each KO payout (since the payout includes
    # the original ante coming back, which was already part of total
    # worth before settlement).
    from datetime import datetime as _dt, timezone as _tz
    kickoffs = []
    for r in fix_rows:
        ts = _parse_ts(r["kick_off"]) or _dt(2099, 1, 1, tzinfo=_tz.utc)
        kickoffs.append(ts)

    # Full per-fixture stage map (covers EVERY fixture, settled or not).
    # The fix_rows-derived map above only includes settled rows, which
    # means bet/adjustment txs on still-open KO fixtures would otherwise
    # slip through the skip and drop the chart line at bet-time.
    is_ko_by_fixture = {
        r["id"]: is_knockout_stage(r["stage"] or "")
        for r in conn.execute("SELECT id, stage FROM fixtures").fetchall()
    }

    # Pull each user's KO bet_amount so we can a) net it out of payouts
    # and b) synthesize a -bet delta for KO losses (which have no tx).
    ko_bet_by_user_fixture = {}
    for r in conn.execute(f"""
        SELECT p.user_id, p.fixture_id, p.bet_amount,
               f.result, p.predicted_result
          FROM picks p JOIN fixtures f ON f.id = p.fixture_id
         WHERE p.pool_id = ?
           AND p.bet_amount IS NOT NULL
           AND f.stage IN ({",".join("?" * len(_KNOCKOUT_STAGES))})
    """, (pool_id, *_KNOCKOUT_STAGES)).fetchall():
        ko_bet_by_user_fixture[(r["user_id"], r["fixture_id"])] = {
            "bet":     float(r["bet_amount"] or 0),
            "result":  r["result"],
            "pick":    r["predicted_result"],
        }

    deltas = defaultdict(lambda: defaultdict(float))
    settled_ko_wins = set()    # (uid, fid) — track which KO bets had a payout tx

    for tx in tx_rows:
        uid = tx["user_id"]
        amt = float(tx["amount"])
        fid = tx["fixture_id"]
        is_ko_fix = is_ko_by_fixture.get(fid, False) if fid else False
        try:
            ttype = tx["type"]
        except (KeyError, IndexError):
            ttype = None

        # KO bet ante (negative tx) and its refund (adjustment, positive tx)
        # do NOT change total worth — skip them outright.
        if is_ko_fix:
            if ttype == "bet" or ttype == "adjustment":
                continue
            if ttype == "payout" and amt > 0:
                # Payout = bet × mult. Net change in total worth = payout - bet
                # (the bet portion was already part of total worth before).
                kp = ko_bet_by_user_fixture.get((uid, fid))
                if kp:
                    amt -= kp["bet"]
                settled_ko_wins.add((uid, fid))

        col = fix_id_to_col.get(fid) if fid else None
        if col is None:
            ts = _parse_ts(tx["created_at"])
            if ts is None:
                col = len(kickoffs)
            else:
                col = len(kickoffs)
                for i, ko in enumerate(kickoffs):
                    if ko >= ts:
                        col = i + 1
                        break
        deltas[uid][col] += amt

    # KO LOSSES leave no transaction at settlement, but the user's total
    # worth drops by the ante. Synthesize a -bet delta for any settled KO
    # pick that didn't land in settled_ko_wins above.
    for (uid, fid), kp in ko_bet_by_user_fixture.items():
        if not kp["result"]:
            continue   # not settled yet
        if (uid, fid) in settled_ko_wins:
            continue   # already counted via payout tx
        if kp["pick"] == kp["result"]:
            continue   # win, but somehow no payout — leave to existing path
        col = fix_id_to_col.get(fid)
        if col is None:
            continue   # fixture has no settlement column in this view
        deltas[uid][col] -= kp["bet"]

    # Pull per-user per-fixture picks + bets so the chart tooltip can
    # show what a player did on that specific match.
    pick_rows = conn.execute("""
        SELECT user_id, fixture_id, predicted_result, bet_amount
        FROM picks
        WHERE pool_id = ?
    """, (pool_id,)).fetchall()
    picks_by_user_fixture = {}
    for r in pick_rows:
        picks_by_user_fixture[(r["user_id"], r["fixture_id"])] = {
            "pick":       r["predicted_result"],
            "bet_amount": float(r["bet_amount"]) if r["bet_amount"] is not None else None,
        }

    # Also fetch each fixture's final result + scores for the tooltip line.
    fix_summary = {}
    for r in fix_rows:
        fix_summary[r["id"]] = {
            "home": r["home_team"], "away": r["away_team"],
            "stage": r["stage"] or "",
        }
    fix_full = conn.execute("""
        SELECT id, home_score, away_score, result FROM fixtures
        WHERE id IN ({})
    """.format(",".join("?" * len(fix_rows))) if fix_rows else "SELECT 0 AS id, 0 AS home_score, 0 AS away_score, '' AS result WHERE 1=0",
        [r["id"] for r in fix_rows]
    ).fetchall() if fix_rows else []
    for r in fix_full:
        fix_summary[r["id"]].update({
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "result":     r["result"],
        })
    conn.close()

    players = []
    for m in member_rows:
        uid = m["user_id"]
        running = start_balance
        cum = [round(running, 2)]
        details = [None]   # index 0 = "Start"; no match details
        for c in range(1, n_cols):
            delta = deltas[uid].get(c, 0.0)
            running += delta
            cum.append(round(running, 2))
            fid = fix_rows[c - 1]["id"]
            fs = fix_summary.get(fid, {})
            pick_info = picks_by_user_fixture.get((uid, fid))
            details.append({
                "label":  f"{(fs.get('home') or '?')[:3].upper()} v {(fs.get('away') or '?')[:3].upper()}",
                "home":   fs.get("home"), "away": fs.get("away"),
                "home_score": fs.get("home_score"), "away_score": fs.get("away_score"),
                "result": fs.get("result"),
                "pick":   pick_info["pick"] if pick_info else None,
                "bet":    pick_info["bet_amount"] if pick_info else None,
                "delta":  round(delta, 2),
            })
        players.append({
            "user_id":      uid,
            "display_name": m["display_name"],
            "team_name":    m["team_name"],
            "email":        m["email"],
            "cumulative":   cum,
            "details":      details,
            "final_score":  round(running, 2),
        })
    players.sort(key=lambda p: p["final_score"], reverse=True)
    return {
        "fixtures":         fixtures_out,
        "players":          players[:max_players],
        "total_players":    len(players),
        "starting_balance": start_balance,
    }


def get_pool_balance_timeline(pool_id, max_players=20):
    """
    Return per-player event series for the stats chart.

    Each member's line is a sequence of {t, y, kind, label} events:
        t      — UNIX ms timestamp (used by Chart.js linear x-scale)
        y      — running balance AFTER this event
        kind   — 'start' | 'payout' | 'spy' | 'bet' | 'adjustment'
        label  — short tooltip-friendly description (e.g. 'MEX v CAN +$5')

    Events are sorted chronologically. The first event for every player is a
    synthetic 'start' carrying the pool's starting_balance, anchored to the
    earliest event timestamp across the whole pool (so all lines share a
    visible left edge).

    Payouts use the related fixture's kick_off + 3h as the event time (not
    transactions.created_at) so late scoring still plots at the right point
    on the time axis. Bets, spies, and adjustments use the transaction's
    own created_at.
    """
    from collections import defaultdict
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    conn = get_db()

    sc = conn.execute(
        "SELECT starting_balance FROM scoring_config ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    start_balance = float((sc and sc["starting_balance"]) or 100)

    tx_rows = conn.execute("""
        SELECT t.user_id, t.fixture_id, t.type, t.amount, t.created_at,
               f.kick_off, f.home_team, f.away_team
        FROM transactions t
        LEFT JOIN fixtures f ON f.id = t.fixture_id
        WHERE t.pool_id = ?
    """, (pool_id,)).fetchall()

    member_rows = conn.execute("""
        SELECT pm.user_id, u.display_name
        FROM pool_members pm JOIN users u ON u.id = pm.user_id
        WHERE pm.pool_id = ?
    """, (pool_id,)).fetchall()
    conn.close()

    if not tx_rows or not member_rows:
        return {"players": [], "total_players": len(member_rows),
                "starting_balance": start_balance, "t_start": None, "t_end": None}

    def _abbr(team):
        return (team or "")[:3].upper()

    # Bucket events per user, computing each event's timestamp + label.
    by_user = defaultdict(list)   # uid → [(t_ms, amount, kind, label)]
    for r in tx_rows:
        uid  = r["user_id"]
        ttype = r["type"] or ""
        amt  = float(r["amount"] or 0)

        # Anchor: payouts use fixture kick_off + 3h; everything else uses
        # the transaction's own created_at.
        if ttype == "payout" and r["kick_off"]:
            ev_dt = _parse_ts(r["kick_off"])
            if ev_dt is not None:
                ev_dt = ev_dt + _td(hours=3)
        else:
            ev_dt = _parse_ts(r["created_at"])
        if ev_dt is None:
            continue
        t_ms = int(ev_dt.timestamp() * 1000)

        if ttype == "payout":
            ha, aa = _abbr(r["home_team"]), _abbr(r["away_team"])
            sign = "+" if amt >= 0 else "-"
            label = f"{ha} v {aa}  {sign}${abs(amt):.0f}"
        elif ttype == "bet":
            ha, aa = _abbr(r["home_team"]), _abbr(r["away_team"])
            label = f"bet on {ha} v {aa}  -${abs(amt):.0f}"
        elif ttype == "spy":
            label = f"spy  -${abs(amt):.0f}"
        elif ttype == "adjustment":
            label = f"adj  {'+' if amt >= 0 else '-'}${abs(amt):.0f}"
        else:
            label = f"{ttype}  ${amt:.0f}"

        by_user[uid].append((t_ms, amt, ttype, label))

    # Earliest event across the whole pool — used as the anchor for the
    # synthetic Start point on every line, so lines share a left edge.
    all_ts = [t for events in by_user.values() for (t, _, _, _) in events]
    if not all_ts:
        return {"players": [], "total_players": len(member_rows),
                "starting_balance": start_balance, "t_start": None, "t_end": None}
    t_start = min(all_ts) - 6 * 60 * 60 * 1000   # 6h padding before first event
    t_end   = max(all_ts)

    # Build each player's event list with running balance
    user_names = {m["user_id"]: m["display_name"] for m in member_rows}
    players_out = []
    for uid, name in user_names.items():
        events = sorted(by_user.get(uid, []))
        series = [{
            "t":     t_start,
            "y":     round(start_balance, 2),
            "kind":  "start",
            "label": "Start",
        }]
        running = start_balance
        for t_ms, amt, kind, label in events:
            running += amt
            series.append({
                "t":     t_ms,
                "y":     round(running, 2),
                "kind":  kind,
                "label": label,
            })
        players_out.append({
            "user_id":      uid,
            "display_name": name,
            "events":       series,
            "final_score":  round(running, 2),
        })

    players_out.sort(key=lambda p: p["final_score"], reverse=True)
    return {
        "players":          players_out[:max_players],
        "total_players":    len(players_out),
        "starting_balance": start_balance,
        "t_start":          t_start,
        "t_end":            t_end,
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


# ── Live-match chat ────────────────────────────────────────────────────────

def add_chat_message(message_id, user_id, pool_id, fixture_id, body,
                     posted_minute=None):
    """Insert one chat message. Returns the inserted row (joined with the
    sender's display name + team_name) ready for JSON serialisation.
    posted_minute is the live game minute at the moment the message was
    posted — stamped so the UI can show "23'" rather than relative wall
    time that goes stale instantly."""
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_messages (id, pool_id, fixture_id, user_id, body, posted_minute) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, pool_id, fixture_id, user_id, body, posted_minute)
    )
    conn.commit()
    row = conn.execute("""
        SELECT cm.id, cm.user_id, cm.pool_id, cm.fixture_id,
               cm.body, cm.created_at, cm.posted_minute,
               u.display_name, u.team_name, u.email,
               p.predicted_result, p.bet_amount,
               0 AS score, 0 AS my_vote
        FROM chat_messages cm
        JOIN users u ON u.id = cm.user_id
        LEFT JOIN picks p ON p.user_id = cm.user_id
                          AND p.pool_id = cm.pool_id
                          AND p.fixture_id = cm.fixture_id
        WHERE cm.id = ?
    """, (message_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_chat_messages_since(pool_id, fixture_id, since_iso=None, limit=100,
                            viewer_id=None):
    """Return chat messages for a (pool, fixture) created after since_iso
    (or all if None). Excludes soft-deleted rows. Each row carries
    `score` (net up-vs-down sum, never shown to users — purely drives
    sizing) and `my_vote` (-1/0/+1) for the viewer."""
    conn = get_db()
    base = """
        SELECT cm.id, cm.user_id, cm.body, cm.created_at, cm.posted_minute,
               u.display_name, u.team_name, u.email,
               p.predicted_result, p.bet_amount,
               COALESCE((SELECT SUM(vote) FROM chat_votes WHERE message_id=cm.id), 0) AS score,
               COALESCE((SELECT vote FROM chat_votes WHERE message_id=cm.id AND user_id=?), 0) AS my_vote
        FROM chat_messages cm
        JOIN users u ON u.id = cm.user_id
        LEFT JOIN picks p ON p.user_id = cm.user_id
                          AND p.pool_id = cm.pool_id
                          AND p.fixture_id = cm.fixture_id
        WHERE cm.pool_id=? AND cm.fixture_id=?
          AND cm.deleted_at IS NULL
    """
    args = [viewer_id or "", pool_id, fixture_id]
    if since_iso:
        base += " AND cm.created_at > ?"; args.append(since_iso)
    base += " ORDER BY cm.created_at ASC LIMIT ?"
    args.append(limit)
    rows = conn.execute(base, tuple(args)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_chat_vote_states(pool_id, fixture_id, viewer_id, limit=200):
    """Return a {message_id: {score, my_vote}} map for the most recent N
    non-deleted messages in this fixture. The chat poller calls this each
    tick so size + button state on *existing* rendered messages stays in
    sync when other users vote."""
    conn = get_db()
    rows = conn.execute("""
        SELECT cm.id AS id,
               COALESCE((SELECT SUM(vote) FROM chat_votes WHERE message_id=cm.id), 0) AS score,
               COALESCE((SELECT vote FROM chat_votes WHERE message_id=cm.id AND user_id=?), 0) AS my_vote
        FROM chat_messages cm
        WHERE cm.pool_id=? AND cm.fixture_id=? AND cm.deleted_at IS NULL
        ORDER BY cm.created_at DESC
        LIMIT ?
    """, (viewer_id or "", pool_id, fixture_id, limit)).fetchall()
    conn.close()
    return {r["id"]: {"score": int(r["score"]), "my_vote": int(r["my_vote"])} for r in rows}


def get_chat_message(message_id):
    """Tiny lookup — used by the vote endpoint to refuse self-votes."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, user_id FROM chat_messages WHERE id=? AND deleted_at IS NULL",
        (message_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def cast_chat_vote(message_id, user_id, vote):
    """vote is +1, -1, or 0.
    - 0 (or repeat of the existing vote) → delete the user's row.
    - ±1 with no prior row → INSERT.
    - ±1 flipping a prior row → UPDATE.
    Returns {my_vote, score}."""
    conn = get_db()
    existing = conn.execute(
        "SELECT vote FROM chat_votes WHERE message_id=? AND user_id=?",
        (message_id, user_id)
    ).fetchone()
    if vote == 0 or (existing and int(existing["vote"]) == int(vote)):
        conn.execute("DELETE FROM chat_votes WHERE message_id=? AND user_id=?",
                     (message_id, user_id))
        my_vote = 0
    elif existing:
        conn.execute(
            "UPDATE chat_votes SET vote=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE message_id=? AND user_id=?",
            (vote, message_id, user_id)
        )
        my_vote = vote
    else:
        conn.execute(
            "INSERT INTO chat_votes(message_id, user_id, vote) VALUES(?, ?, ?)",
            (message_id, user_id, vote)
        )
        my_vote = vote
    row = conn.execute(
        "SELECT COALESCE(SUM(vote), 0) AS s FROM chat_votes WHERE message_id=?",
        (message_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return {"my_vote": int(my_vote), "score": int(dict(row)["s"])}


def soft_delete_chat_message(message_id):
    """Admin moderation — mark one message deleted. Already-deleted
    rows are no-ops."""
    conn = get_db()
    conn.execute(
        "UPDATE chat_messages SET deleted_at = CURRENT_TIMESTAMP "
        "WHERE id=? AND deleted_at IS NULL",
        (message_id,)
    )
    conn.commit()
    conn.close()


def soft_delete_fixture_chat(fixture_id):
    """Called from process_fixture_result when a fixture FINISHES.
    Hides all that fixture's chat from future GETs without erasing
    history."""
    conn = get_db()
    conn.execute(
        "UPDATE chat_messages SET deleted_at = CURRENT_TIMESTAMP "
        "WHERE fixture_id=? AND deleted_at IS NULL",
        (fixture_id,)
    )
    conn.commit()
    conn.close()


def set_user_chat_banned(user_id, banned):
    """Admin-only chat ban toggle. Banned users can still read but POST
    is rejected at the route layer."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET chat_banned=? WHERE id=?",
        (1 if banned else 0, user_id)
    )
    conn.commit()
    conn.close()


def is_user_chat_banned(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(chat_banned, 0) AS b FROM users WHERE id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["b"])
