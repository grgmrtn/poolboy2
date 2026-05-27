"""
seed_test_scoring.py — create a fake pool with completed fixtures and scored picks.

Run from inside your worldcup-pool folder:
    python3 seed_test_scoring.py

Then visit /home and join "Test Scoring Pool" to see the results.
Or run the query at the bottom directly in sqlite3:
    sqlite3 instance/pool.db < scoring_check.sql
"""

import sys
import os
import uuid
import sqlite3

# Make sure we can import from the app folder
sys.path.insert(0, os.path.dirname(__file__))
import database as db
from werkzeug.security import generate_password_hash

# ── Config ─────────────────────────────────────────────────────────────────
POINTS_WIN  = 3
POINTS_DRAW = 1
POINTS_LOSS = 0

# 16 fake completed fixtures with known results
# result: H = home win, A = away win, D = draw
FAKE_FIXTURES = [
    ("Brazil",      "Germany",      "H"),   # Brazil win
    ("France",      "Argentina",    "D"),   # Draw
    ("Spain",       "England",      "A"),   # England win
    ("Portugal",    "Netherlands",  "H"),   # Portugal win
    ("Belgium",     "Uruguay",      "D"),   # Draw
    ("Croatia",     "Morocco",      "A"),   # Morocco win
    ("Japan",       "Senegal",      "H"),   # Japan win
    ("USA",         "Mexico",       "D"),   # Draw
    ("Germany",     "France",       "A"),   # France win
    ("Brazil",      "Argentina",    "H"),   # Brazil win
    ("England",     "Portugal",     "D"),   # Draw
    ("Spain",       "Netherlands",  "H"),   # Spain win
    ("Morocco",     "Belgium",      "A"),   # Belgium win
    ("Uruguay",     "Croatia",      "H"),   # Uruguay win
    ("Senegal",     "USA",          "D"),   # Draw
    ("Mexico",      "Japan",        "A"),   # Japan win
]

# 4 fake users with different prediction strategies
# Each entry is (display_name, email, predictions_list)
# predictions must align with FAKE_FIXTURES order
FAKE_USERS = [
    ("Alice",   "alice_test@test.com",   ["H","D","A","H","D","A","H","D","A","H","D","H","A","H","D","A"]),  # Perfect
    ("Bob",     "bob_test@test.com",     ["A","H","H","A","H","H","A","H","H","A","H","A","H","A","H","H"]),  # All wrong
    ("Carol",   "carol_test@test.com",   ["H","D","H","H","D","A","H","D","A","H","D","H","A","H","D","A"]),  # Mostly right
    ("Dave",    "dave_test@test.com",    ["H","H","A","H","H","A","H","H","A","H","H","H","A","H","H","A"]),  # Mixed
]


def run():
    db.init_db()
    conn = db.get_db()

    # ── Scoring config ──────────────────────────────────────────────────────
    # Find or create the admin user to attach the config to
    admin = conn.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    admin_id = admin["id"] if admin else str(uuid.uuid4())

    config_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO scoring_config (id, points_win, points_draw, points_loss, updated_by)
        VALUES (?,?,?,?,?)
    """, (config_id, POINTS_WIN, POINTS_DRAW, POINTS_LOSS, admin_id))
    print(f"[config] Win={POINTS_WIN}, Draw={POINTS_DRAW}, Loss={POINTS_LOSS}")

    # ── Pool ────────────────────────────────────────────────────────────────
    pool_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO pools (id, name, description, is_public) VALUES (?,?,?,1)",
        (pool_id, "Test Scoring Pool", "Fake pool with 16 completed fixtures")
    )
    print(f"[pool] Created 'Test Scoring Pool' id={pool_id}")

    # ── Fixtures ────────────────────────────────────────────────────────────
    import hashlib
    fixture_ids = []
    for i, (home, away, result) in enumerate(FAKE_FIXTURES):
        fid = str(uuid.UUID(hashlib.md5(f"test-{home}-{away}".encode()).hexdigest()))
        fixture_ids.append(fid)
        # Score 3-0, 1-1, or 0-1 to match result cleanly
        if result == "H":
            hs, as_ = 2, 0
        elif result == "A":
            hs, as_ = 0, 1
        else:
            hs, as_ = 1, 1
        conn.execute("""
            INSERT INTO fixtures (id, home_team, away_team, home_flag_code, away_flag_code,
                                  kick_off, stage, home_score, away_score, result)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                result=excluded.result
        """, (fid, home, away, "un", "un",
              f"2026-06-{11+i//2:02d}T{18 + (i%2)*3:02d}:00:00",
              "Test Group", hs, as_, result))
    print(f"[fixtures] Inserted {len(fixture_ids)} completed fixtures")

    # ── Users, picks, scores ────────────────────────────────────────────────
    for display_name, email, predictions in FAKE_USERS:
        # Create user if not already there
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            user_id = existing["id"]
            print(f"[user] Reusing existing user {email}")
        else:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, display_name, email, password_hash) VALUES (?,?,?,?)",
                (user_id, display_name, email, generate_password_hash("testpass"))
            )

        # Join pool
        try:
            conn.execute(
                "INSERT INTO pool_members (id, user_id, pool_id) VALUES (?,?,?)",
                (str(uuid.uuid4()), user_id, pool_id)
            )
        except sqlite3.IntegrityError:
            pass

        # Insert picks and score them
        correct = 0
        total_pts = 0
        for fid, pred, (_, _, actual) in zip(fixture_ids, predictions, FAKE_FIXTURES):
            pick_id = str(uuid.uuid4())
            try:
                conn.execute("""
                    INSERT INTO picks (id, user_id, pool_id, fixture_id, predicted_result)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(user_id, pool_id, fixture_id) DO UPDATE SET
                        predicted_result=excluded.predicted_result
                """, (pick_id, user_id, pool_id, fid, pred))
            except Exception:
                # Fetch the real pick_id if it already existed
                row = conn.execute(
                    "SELECT id FROM picks WHERE user_id=? AND pool_id=? AND fixture_id=?",
                    (user_id, pool_id, fid)
                ).fetchone()
                pick_id = row["id"]

            pts = POINTS_WIN if pred == actual else POINTS_LOSS
            total_pts += pts
            if pred == actual:
                correct += 1

            try:
                conn.execute("""
                    INSERT INTO score_log (id, user_id, pool_id, pick_id, points_awarded)
                    VALUES (?,?,?,?,?)
                """, (str(uuid.uuid4()), user_id, pool_id, pick_id, pts))
            except sqlite3.IntegrityError:
                pass  # Already scored

        print(f"[user] {display_name:8s}  correct={correct:2d}/16  pts={total_pts}")

    conn.commit()
    conn.close()

    # ── Print leaderboard query result ──────────────────────────────────────
    print("\n── Leaderboard ───────────────────────────────────")
    conn2 = db.get_db()
    rows = conn2.execute("""
        SELECT u.display_name,
               COUNT(p.id)                            AS picks,
               SUM(CASE WHEN p.predicted_result = f.result THEN 1 ELSE 0 END) AS correct,
               COALESCE(SUM(sl.points_awarded), 0)    AS total_pts
        FROM pool_members pm
        JOIN users u        ON u.id  = pm.user_id
        JOIN picks p        ON p.user_id = pm.user_id AND p.pool_id = pm.pool_id
        JOIN fixtures f     ON f.id  = p.fixture_id
        LEFT JOIN score_log sl ON sl.pick_id = p.id
        WHERE pm.pool_id = ?
        GROUP BY u.id
        ORDER BY total_pts DESC
    """, (pool_id,)).fetchall()
    conn2.close()

    print(f"  {'Name':<10} {'Picks':>5} {'Correct':>7} {'Points':>7}")
    print(f"  {'-'*10} {'-'*5} {'-'*7} {'-'*7}")
    for r in rows:
        print(f"  {r['display_name']:<10} {r['picks']:>5} {r['correct']:>7} {r['total_pts']:>7}")

    print(f"\nPool id: {pool_id}")
    print("Login as alice_test@test.com / testpass to see Alice's view (perfect score).")


if __name__ == "__main__":
    run()
