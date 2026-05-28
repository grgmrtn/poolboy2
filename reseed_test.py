"""
reseed_test.py — create a realistic test scenario for the economy-based picking system.

Creates 4 users, 1 pool, and a mix of:
  - Completed group stage fixtures (with payouts applied)
  - Locked/live group stage fixtures (past 15-min lock window, picks visible)
  - Upcoming group stage fixtures (picks hidden, spy buttons visible)
  - Completed knockout fixtures (with bet-based payouts)
  - Upcoming knockout fixture (KO betting form visible)
  - Spy purchases (some users have paid to reveal other picks)

Run from the project root:
    python3 reseed_test.py

Accounts created (password: testpass for all):
    alice_econ@test.com  — good predictor, balance boosted
    bob_econ@test.com    — bad predictor, balance depleted
    carol_econ@test.com  — moderate, bought spies
    dave_econ@test.com   — high-roller KO bettor

Login as any of the above to see the pool from their perspective.
Pool name: "Economy Test Pool"
"""

import sys, os, uuid, hashlib
sys.path.insert(0, os.path.dirname(__file__))

import database as db
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta

PASSWORD = "testpass"
POOL_NAME = "Economy Test Pool"

NOW = datetime.utcnow()


def stable_id(key):
    return str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))


def mock_fid(home, away):
    return str(uuid.UUID(hashlib.md5(f"{home}-{away}".encode()).hexdigest()))


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def ko_stage(stage):
    return stage not in (
        "Group A","Group B","Group C","Group D","Group E","Group F",
        "Group G","Group H","Group I","Group J","Group K","Group L",
    )


FIXTURES = [
    {
        "id":    mock_fid("Brazil", "Colombia"),
        "home":  "Brazil", "away": "Colombia",
        "stage": "Group E",
        "ko":    fmt(NOW - timedelta(days=5, hours=3)),
        "hs": 2, "as_": 1,
    },
    {
        "id":    mock_fid("France", "Belgium"),
        "home":  "France", "away": "Belgium",
        "stage": "Group D",
        "ko":    fmt(NOW - timedelta(days=4, hours=2)),
        "hs": 1, "as_": 1,
    },
    {
        "id":    mock_fid("United States", "Panama"),
        "home":  "United States", "away": "Panama",
        "stage": "Group C",
        "ko":    fmt(NOW - timedelta(days=3, hours=4)),
        "hs": 0, "as_": 2,
    },
    {
        "id":    mock_fid("Portugal", "Germany"),
        "home":  "Portugal", "away": "Germany",
        "stage": "Group G",
        "ko":    fmt(NOW - timedelta(days=2, hours=5)),
        "hs": 3, "as_": 0,
    },
    {
        "id":    mock_fid("Argentina", "Chile"),
        "home":  "Argentina", "away": "Chile",
        "stage": "Group A",
        "ko":    fmt(NOW - timedelta(minutes=20)),
        "hs": None, "as_": None,
    },
    {
        "id":    mock_fid("Japan", "Saudi Arabia"),
        "home":  "Japan", "away": "Saudi Arabia",
        "stage": "Group I",
        "ko":    fmt(NOW - timedelta(minutes=10)),
        "hs": None, "as_": None,
    },
    {
        "id":    mock_fid("Colombia", "Venezuela"),
        "home":  "Colombia", "away": "Venezuela",
        "stage": "Group E",
        "ko":    fmt(NOW + timedelta(hours=3)),
        "hs": None, "as_": None,
    },
    {
        "id":    mock_fid("South Korea", "Ghana"),
        "home":  "South Korea", "away": "Ghana",
        "stage": "Group I",
        "ko":    fmt(NOW + timedelta(hours=6)),
        "hs": None, "as_": None,
    },
    {
        "id":    mock_fid("Honduras", "Bosnia-Herzegovina"),
        "home":  "Honduras", "away": "Bosnia-Herzegovina",
        "stage": "Group C",
        "ko":    fmt(NOW + timedelta(days=1)),
        "hs": None, "as_": None,
    },
    {
        "id":    stable_id("econ-ko-Brazil-France-R16"),
        "home":  "Brazil", "away": "France",
        "stage": "Round of 16",
        "ko":    fmt(NOW - timedelta(days=1)),
        "hs": 2, "as_": 0,
    },
    {
        "id":    stable_id("econ-ko-Spain-Germany-QF"),
        "home":  "Spain", "away": "Germany",
        "stage": "Quarter-Finals",
        "ko":    fmt(NOW + timedelta(hours=12)),
        "hs": None, "as_": None,
    },
]

USERS = [
    ("Alice (Econ)", "alice_econ@test.com", [
        "H", "D", "A", "H",
        "H", "D",
        "H", "H", None,
        ("H", 20.0),
        None,
    ]),
    ("Bob (Econ)", "bob_econ@test.com", [
        "A", "H", "H", "A",
        "D", "H",
        "A", "A", "H",
        ("A", 10.0),
        None,
    ]),
    ("Carol (Econ)", "carol_econ@test.com", [
        "H", "D", "H", "H",
        "H", None,
        "D", "H", "A",
        ("H", 5.0),
        None,
    ]),
    ("Dave (Econ)", "dave_econ@test.com", [
        "H", "H", "A", "H",
        "A", "D",
        None, "A", "H",
        ("H", 30.0),
        ("A", 15.0),
    ]),
]

SPY_PURCHASES = [
    ("carol_econ@test.com", "alice_econ@test.com", 6),
    ("carol_econ@test.com", "bob_econ@test.com",   6),
    ("carol_econ@test.com", "alice_econ@test.com", 7),
]


def run():
    db.init_db()
    conn = db.get_db()

    old_pool = conn.execute(
        "SELECT id FROM pools WHERE name=?", (POOL_NAME,)
    ).fetchone()
    if old_pool:
        pid = old_pool["id"]
        conn.execute("DELETE FROM spy_log WHERE pool_id=?",       (pid,))
        conn.execute("DELETE FROM transactions WHERE pool_id=?",  (pid,))
        conn.execute("DELETE FROM score_log WHERE pool_id=?",     (pid,))
        conn.execute("DELETE FROM picks WHERE pool_id=?",         (pid,))
        conn.execute("DELETE FROM pool_members WHERE pool_id=?",  (pid,))
        conn.execute("DELETE FROM pools WHERE id=?",              (pid,))
        conn.commit()
        print(f"[clean] Removed previous '{POOL_NAME}' pool")

    pool_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO pools (id, name, description, is_public) VALUES (?,?,?,1)",
        (pool_id, POOL_NAME, "Economy system test pool with all feature scenarios")
    )
    print(f"[pool] Created '{POOL_NAME}' id={pool_id}")

    from fixtures import get_flag_code
    for f in FIXTURES:
        hs, as_ = f["hs"], f["as_"]
        result = None
        if hs is not None and as_ is not None:
            result = "H" if hs > as_ else ("A" if as_ > hs else "D")
        conn.execute("""
            INSERT INTO fixtures (id, home_team, away_team, home_flag_code, away_flag_code,
                                  kick_off, stage, home_score, away_score, result)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                kick_off=excluded.kick_off,
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                result=excluded.result,
                stage=excluded.stage
        """, (
            f["id"], f["home"], f["away"],
            get_flag_code(f["home"]), get_flag_code(f["away"]),
            f["ko"], f["stage"], hs, as_, result
        ))
    print(f"[fixtures] Upserted {len(FIXTURES)} fixtures")

    admin = conn.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    admin_id = admin["id"] if admin else str(uuid.uuid4())
    cfg_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO scoring_config (id, points_win, points_draw, points_loss, updated_by,
                                    starting_balance, group_win_payout, group_draw_payout,
                                    group_loss_payout, spy_base_cost, spy_increment,
                                    knockout_flat_payout_multiplier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (cfg_id, 3, 1, 0, admin_id,
          100.0, 5.0, 2.0, 0.0, 1.0, 1.0, 2.0))
    config = {
        "starting_balance": 100.0,
        "group_win_payout": 5.0,
        "group_draw_payout": 2.0,
        "group_loss_payout": 0.0,
        "spy_base_cost": 1.0,
        "spy_increment": 1.0,
        "knockout_flat_payout_multiplier": 2.0,
    }

    user_ids = {}
    for display_name, email, picks in USERS:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            user_id = existing["id"]
            print(f"[user] Reusing {email}")
        else:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, display_name, email, password_hash) VALUES (?,?,?,?)",
                (user_id, display_name, email, generate_password_hash(PASSWORD))
            )
        user_ids[email] = user_id

        balance = config["starting_balance"]
        try:
            conn.execute(
                "INSERT INTO pool_members (id, user_id, pool_id, has_paid, balance) VALUES (?,?,?,1,?)",
                (str(uuid.uuid4()), user_id, pool_id, balance)
            )
        except Exception:
            conn.execute(
                "UPDATE pool_members SET balance=? WHERE user_id=? AND pool_id=?",
                (balance, user_id, pool_id)
            )

        for i, pick_data in enumerate(picks):
            if pick_data is None:
                continue
            f    = FIXTURES[i]
            fid  = f["id"]
            stage = f["stage"]
            is_ko = ko_stage(stage)
            hs, as_ = f["hs"], f["as_"]
            result = None
            if hs is not None and as_ is not None:
                result = "H" if hs > as_ else ("A" if as_ > hs else "D")

            if is_ko:
                prediction, bet_amount = pick_data
                conn.execute("""
                    INSERT OR IGNORE INTO picks
                        (id, user_id, pool_id, fixture_id, predicted_result, bet_amount)
                    VALUES (?,?,?,?,?,?)
                """, (str(uuid.uuid4()), user_id, pool_id, fid, prediction, bet_amount))
                conn.execute("""
                    INSERT INTO transactions
                        (id, user_id, pool_id, fixture_id, type, amount, description)
                    VALUES (?,?,?,?,?,?,?)
                """, (str(uuid.uuid4()), user_id, pool_id, fid, "bet",
                      -bet_amount, f"KO bet · ${bet_amount:.2f}"))
                conn.execute(
                    "UPDATE pool_members SET balance = balance - ? WHERE user_id=? AND pool_id=?",
                    (bet_amount, user_id, pool_id)
                )
                if result and prediction == result:
                    payout = bet_amount * config["knockout_flat_payout_multiplier"]
                    conn.execute("""
                        INSERT INTO transactions
                            (id, user_id, pool_id, fixture_id, type, amount, description)
                        VALUES (?,?,?,?,?,?,?)
                    """, (str(uuid.uuid4()), user_id, pool_id, fid, "payout",
                          payout, f"KO win · ${bet_amount:.2f}×{config['knockout_flat_payout_multiplier']}=${payout:.2f}"))
                    conn.execute(
                        "UPDATE pool_members SET balance = balance + ? WHERE user_id=? AND pool_id=?",
                        (payout, user_id, pool_id)
                    )
            else:
                prediction = pick_data
                conn.execute("""
                    INSERT OR IGNORE INTO picks
                        (id, user_id, pool_id, fixture_id, predicted_result)
                    VALUES (?,?,?,?,?)
                """, (str(uuid.uuid4()), user_id, pool_id, fid, prediction))
                if result:
                    if prediction == result and result == "D":
                        payout = config["group_draw_payout"]
                        desc   = f"Group draw — correct · +${payout:.2f}"
                    elif prediction == result:
                        payout = config["group_win_payout"]
                        desc   = f"Group win — correct · +${payout:.2f}"
                    else:
                        payout = config["group_loss_payout"]
                        desc   = f"Group miss · {payout:+.2f}"
                    conn.execute("""
                        INSERT INTO transactions
                            (id, user_id, pool_id, fixture_id, type, amount, description)
                        VALUES (?,?,?,?,?,?,?)
                    """, (str(uuid.uuid4()), user_id, pool_id, fid, "payout", payout, desc))
                    conn.execute(
                        "UPDATE pool_members SET balance = balance + ? WHERE user_id=? AND pool_id=?",
                        (payout, user_id, pool_id)
                    )

    conn.commit()
    print(f"[picks] Inserted picks and transactions for all users")

    for idx, (buyer_email, target_email, fix_idx) in enumerate(SPY_PURCHASES):
        buyer_id  = user_ids.get(buyer_email)
        target_id = user_ids.get(target_email)
        fid       = FIXTURES[fix_idx]["id"]
        cost      = config["spy_base_cost"] + config["spy_increment"] * idx
        if not buyer_id or not target_id:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO spy_log
                (id, buyer_id, target_id, pool_id, fixture_id, cost)
            VALUES (?,?,?,?,?,?)
        """, (str(uuid.uuid4()), buyer_id, target_id, pool_id, fid, cost))
        conn.execute("""
            INSERT INTO transactions
                (id, user_id, pool_id, fixture_id, type, amount, description)
            VALUES (?,?,?,?,?,?,?)
        """, (str(uuid.uuid4()), buyer_id, pool_id, fid, "spy",
              -cost, f"Spy: {target_email[:20]}"))
        conn.execute(
            "UPDATE pool_members SET balance = balance - ? WHERE user_id=? AND pool_id=?",
            (cost, buyer_id, pool_id)
        )
    conn.commit()
    print(f"[spy] Recorded {len(SPY_PURCHASES)} spy purchases")

    print(f"\n── Economy Test Pool (id={pool_id}) ────────────────")
    rows = conn.execute("""
        SELECT u.display_name, COALESCE(pm.balance,100) AS balance,
               COUNT(t.id) AS txns
        FROM pool_members pm
        JOIN users u ON u.id = pm.user_id
        LEFT JOIN transactions t ON t.user_id=pm.user_id AND t.pool_id=pm.pool_id
        WHERE pm.pool_id=?
        GROUP BY u.id
        ORDER BY balance DESC
    """, (pool_id,)).fetchall()
    print(f"  {'Name':<20} {'Balance':>10} {'Txns':>6}")
    print(f"  {'-'*20} {'-'*10} {'-'*6}")
    for r in rows:
        print(f"  {r['display_name']:<20} ${r['balance']:>9.2f} {r['txns']:>6}")
    print()
    print("Login: alice_econ@test.com / testpass  (all 4 users use 'testpass')")
    print(f"Pool URL: /pool/{pool_id}")

    conn.close()


if __name__ == "__main__":
    run()
