"""
database.py — all database setup and query helpers.

Uses Python's built-in sqlite3 module (no external ORM needed).
The database file is created automatically at first run.
"""

import sqlite3
import os
from datetime import datetime

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
        self._cur.execute(sql.replace("?", "%s"), params or ())
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
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            is_public   INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Join table: which users belong to which pools.
    c.execute("""
        CREATE TABLE IF NOT EXISTS pool_members (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id),
            pool_id    TEXT NOT NULL REFERENCES pools(id),
            joined_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, pool_id)
        )
    """)

    # One row per match. home_score/away_score/result are NULL until played.
    # flag_code is the 2-letter ISO country code used by flagcdn.com.
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
            result         TEXT
        )
    """)

    # A user's prediction for one fixture within a specific pool.
    # predicted_result: "H" = home win, "A" = away win, "D" = draw.
    c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id               TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL REFERENCES users(id),
            pool_id          TEXT NOT NULL REFERENCES pools(id),
            fixture_id       TEXT NOT NULL REFERENCES fixtures(id),
            predicted_result TEXT NOT NULL CHECK(predicted_result IN ('H','A','D')),
            submitted_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, pool_id, fixture_id)
        )
    """)

    # Admin sets how many points a correct win/draw/loss prediction scores.
    # New rows are inserted rather than updating, preserving history.
    c.execute("""
        CREATE TABLE IF NOT EXISTS scoring_config (
            id           TEXT PRIMARY KEY,
            points_win   INTEGER NOT NULL DEFAULT 3,
            points_draw  INTEGER NOT NULL DEFAULT 1,
            points_loss  INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_by   TEXT REFERENCES users(id)
        )
    """)

    # One row per pick that has been scored, written by the scoring job.
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

    # Migrations: add columns to existing tables that pre-date this schema version.
    if _USE_POSTGRES:
        for sql in [
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS pool_id TEXT",
            "ALTER TABLE scoring_config ADD COLUMN IF NOT EXISTS round_type TEXT",
        ]:
            c.execute(sql)
    else:
        for col_def in ["pool_id TEXT", "round_type TEXT"]:
            try:
                c.execute(f"ALTER TABLE scoring_config ADD COLUMN {col_def}")
            except Exception:
                pass

    conn.commit()
    conn.close()
    print(f"[db] Database ready at {DB_PATH}")


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
    Return the total points a user has earned.
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
    """Return all pools that a user is currently a member of."""
    conn = get_db()
    pools = conn.execute("""
        SELECT p.* FROM pools p
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


def join_pool(member_id, user_id, pool_id):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO pool_members (id, user_id, pool_id) VALUES (?,?,?)",
            (member_id, user_id, pool_id)
        )
        conn.commit()
        print("JOIN SUCCESS:", member_id, user_id, pool_id)
    except _IntegrityError as e:
        print("JOIN FAILED:", e, member_id, user_id, pool_id)
    finally:
        conn.close()


def get_pool_by_id(pool_id):
    """Fetch a single pool row by its ID."""
    conn = get_db()
    pool = conn.execute("SELECT * FROM pools WHERE id=?", (pool_id,)).fetchone()
    conn.close()
    return pool


def create_pool(pool_id, name, description, is_public=1):
    """Insert a new pool."""
    conn = get_db()
    conn.execute(
        "INSERT INTO pools (id, name, description, is_public) VALUES (?,?,?,?)",
        (pool_id, name, description, is_public)
    )
    conn.commit()
    conn.close()


def get_pool_leaderboard(pool_id):
    """
    Return all members of a pool ranked by total points, highest first.
    Each row includes display_name, email, and total_points.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT u.display_name, u.email,
               COALESCE(SUM(sl.points_awarded), 0) AS total_points
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        LEFT JOIN score_log sl ON sl.user_id = pm.user_id AND sl.pool_id = pm.pool_id
        WHERE pm.pool_id = ?
        GROUP BY u.id
        ORDER BY total_points DESC
    """, (pool_id,)).fetchall()
    conn.close()
    return rows


# ── Fixture helpers ────────────────────────────────────────────────────────

def upsert_fixture(fixture):
    """
    Insert or update a fixture row.

    Uses positional ? parameters (more compatible across Python/sqlite3 versions
    than named :param style with ON CONFLICT clauses).

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

def upsert_pick(pick_id, user_id, pool_id, fixture_id, predicted_result):
    """
    Save or update a user's pick for a fixture.
    If they already have a pick for this fixture in this pool, overwrite it.
    """
    conn = get_db()
    conn.execute("""
        INSERT INTO picks (id, user_id, pool_id, fixture_id, predicted_result)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id, pool_id, fixture_id) DO UPDATE SET
            predicted_result = excluded.predicted_result,
            submitted_at     = CURRENT_TIMESTAMP
    """, (pick_id, user_id, pool_id, fixture_id, predicted_result))
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


# ── Scoring config helpers ─────────────────────────────────────────────────

_KNOCKOUT_STAGES = frozenset({
    'Round of 32', 'Round of 16', 'Quarter-Finals',
    'Semi-Finals', 'Third Place', 'Final',
})

def _round_type_for_stage(stage):
    return 'knockout' if (stage or '') in _KNOCKOUT_STAGES else 'group_stage'


def get_scoring_config(pool_id=None, stage=None):
    """
    Return the scoring config for a pool/stage combination.
    Falls back: pool+round_type → pool (any round) → global → hardcoded defaults.
    """
    round_type = _round_type_for_stage(stage)
    conn = get_db()

    if pool_id:
        row = conn.execute("""
            SELECT * FROM scoring_config WHERE pool_id=? AND round_type=?
            ORDER BY updated_at DESC LIMIT 1
        """, (pool_id, round_type)).fetchone()
        if row:
            conn.close(); return dict(row)

        row = conn.execute("""
            SELECT * FROM scoring_config WHERE pool_id=? AND round_type IS NULL
            ORDER BY updated_at DESC LIMIT 1
        """, (pool_id,)).fetchone()
        if row:
            conn.close(); return dict(row)

    row = conn.execute("""
        SELECT * FROM scoring_config WHERE pool_id IS NULL AND round_type IS NULL
        ORDER BY updated_at DESC LIMIT 1
    """).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {"points_win": 3, "points_draw": 1, "points_loss": 0}


def get_active_scoring_config():
    return get_scoring_config()


def save_scoring_config(config_id, points_win, points_draw, points_loss, updated_by,
                        pool_id=None, round_type=None):
    conn = get_db()
    conn.execute("""
        INSERT INTO scoring_config (id, pool_id, round_type, points_win, points_draw, points_loss, updated_by)
        VALUES (?,?,?,?,?,?,?)
    """, (config_id, pool_id, round_type, points_win, points_draw, points_loss, updated_by))
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


# ── Scoring job ────────────────────────────────────────────────────────────

def calculate_scores_for_fixture(fixture_id):
    """
    Score all picks for a completed fixture.
    Uses per-pool, per-round scoring config with fallback to global defaults.
    Idempotent — running it twice won't double-score anyone.
    Returns: number of picks that were newly scored.
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
            pass  # Already scored

    conn.commit()
    conn.close()
    return scored
