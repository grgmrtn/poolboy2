"""
seed_euro2024.py — Seed a Euro 2024 simulation pool.

Creates:
  - 100 simulated player accounts
  - A pool called "Euro 2024 Sim"
  - All Euro 2024 fixtures with real results
  - Random picks for each player
  - Calculates and stores scores for every completed fixture

Run:
    python3 seed_euro2024.py
"""

import uuid
import random
from werkzeug.security import generate_password_hash
import database as db

random.seed(42)

# ── Euro 2024 Groups ───────────────────────────────────────────────────────

GROUPS = {
    "Group A": ["Germany", "Scotland", "Hungary", "Switzerland"],
    "Group B": ["Spain",   "Croatia",  "Italy",   "Albania"],
    "Group C": ["Slovenia","Denmark",  "Serbia",  "England"],
    "Group D": ["Poland",  "Netherlands", "Austria", "France"],
    "Group E": ["Belgium", "Slovakia", "Romania", "Ukraine"],
    "Group F": ["Turkey",  "Georgia",  "Portugal","Czechia"],
}

# ── Full fixture list with results ─────────────────────────────────────────
# Format: (home, away, stage, kick_off, home_score, away_score)
# Results based on Euro 2024 actuals (Germany, June–July 2024)

FIXTURES = [
    # ── Group A ────────────────────────────────────────────────────────
    ("Germany",     "Scotland",    "Group A", "2024-06-14T21:00:00", 5, 1),
    ("Hungary",     "Switzerland", "Group A", "2024-06-15T15:00:00", 1, 3),
    ("Germany",     "Hungary",     "Group A", "2024-06-19T18:00:00", 2, 0),
    ("Scotland",    "Switzerland", "Group A", "2024-06-19T21:00:00", 1, 1),
    ("Switzerland", "Germany",     "Group A", "2024-06-23T21:00:00", 1, 1),
    ("Scotland",    "Hungary",     "Group A", "2024-06-23T21:00:00", 0, 1),

    # ── Group B ────────────────────────────────────────────────────────
    ("Spain",       "Croatia",     "Group B", "2024-06-15T18:00:00", 3, 0),
    ("Italy",       "Albania",     "Group B", "2024-06-15T21:00:00", 2, 1),
    ("Croatia",     "Albania",     "Group B", "2024-06-19T15:00:00", 2, 2),
    ("Spain",       "Italy",       "Group B", "2024-06-20T21:00:00", 1, 0),
    ("Albania",     "Spain",       "Group B", "2024-06-24T21:00:00", 0, 1),
    ("Croatia",     "Italy",       "Group B", "2024-06-24T21:00:00", 1, 1),

    # ── Group C ────────────────────────────────────────────────────────
    ("Slovenia",    "Denmark",     "Group C", "2024-06-16T15:00:00", 1, 1),
    ("Serbia",      "England",     "Group C", "2024-06-16T21:00:00", 0, 1),
    ("Slovenia",    "Serbia",      "Group C", "2024-06-20T15:00:00", 1, 1),
    ("Denmark",     "England",     "Group C", "2024-06-20T18:00:00", 1, 1),
    ("England",     "Slovenia",    "Group C", "2024-06-25T21:00:00", 0, 0),
    ("Denmark",     "Serbia",      "Group C", "2024-06-25T21:00:00", 0, 0),

    # ── Group D ────────────────────────────────────────────────────────
    ("Poland",      "Netherlands", "Group D", "2024-06-16T18:00:00", 1, 2),
    ("Austria",     "France",      "Group D", "2024-06-17T21:00:00", 0, 1),
    ("Poland",      "Austria",     "Group D", "2024-06-21T18:00:00", 1, 3),
    ("Netherlands", "France",      "Group D", "2024-06-21T21:00:00", 0, 0),
    ("Netherlands", "Austria",     "Group D", "2024-06-25T18:00:00", 3, 2),
    ("France",      "Poland",      "Group D", "2024-06-25T18:00:00", 1, 1),

    # ── Group E ────────────────────────────────────────────────────────
    ("Romania",     "Ukraine",     "Group E", "2024-06-17T15:00:00", 3, 0),
    ("Belgium",     "Slovakia",    "Group E", "2024-06-17T18:00:00", 0, 1),
    ("Slovakia",    "Ukraine",     "Group E", "2024-06-21T15:00:00", 2, 1),
    ("Belgium",     "Romania",     "Group E", "2024-06-22T21:00:00", 2, 0),
    ("Slovakia",    "Romania",     "Group E", "2024-06-26T18:00:00", 1, 1),
    ("Ukraine",     "Belgium",     "Group E", "2024-06-26T18:00:00", 0, 0),

    # ── Group F ────────────────────────────────────────────────────────
    ("Turkey",      "Georgia",     "Group F", "2024-06-18T18:00:00", 3, 1),
    ("Portugal",    "Czechia",     "Group F", "2024-06-18T21:00:00", 2, 1),
    ("Georgia",     "Czechia",     "Group F", "2024-06-22T15:00:00", 2, 1),
    ("Turkey",      "Portugal",    "Group F", "2024-06-22T18:00:00", 0, 3),
    ("Georgia",     "Portugal",    "Group F", "2024-06-26T21:00:00", 2, 0),
    ("Czechia",     "Turkey",      "Group F", "2024-06-26T21:00:00", 1, 2),

    # ── Round of 16 ────────────────────────────────────────────────────
    ("Switzerland", "Italy",       "Round of 16", "2024-06-29T18:00:00", 2, 0),
    ("Germany",     "Denmark",     "Round of 16", "2024-06-29T21:00:00", 2, 0),
    ("England",     "Slovakia",    "Round of 16", "2024-06-30T18:00:00", 2, 1),
    ("Spain",       "Georgia",     "Round of 16", "2024-06-30T21:00:00", 4, 1),
    ("France",      "Belgium",     "Round of 16", "2024-07-01T18:00:00", 1, 0),
    ("Portugal",    "Slovenia",    "Round of 16", "2024-07-01T21:00:00", 3, 0),
    ("Romania",     "Netherlands", "Round of 16", "2024-07-02T18:00:00", 0, 3),
    ("Austria",     "Turkey",      "Round of 16", "2024-07-02T21:00:00", 1, 2),

    # ── Quarter-Finals ─────────────────────────────────────────────────
    ("Spain",       "Germany",     "Quarter-Finals", "2024-07-05T21:00:00", 2, 1),
    ("Portugal",    "France",      "Quarter-Finals", "2024-07-05T18:00:00", 0, 0),  # France on pens
    ("England",     "Switzerland", "Quarter-Finals", "2024-07-06T18:00:00", 1, 1),  # England on pens
    ("Netherlands", "Turkey",      "Quarter-Finals", "2024-07-06T21:00:00", 2, 1),

    # ── Semi-Finals ────────────────────────────────────────────────────
    ("Spain",       "France",      "Semi-Finals", "2024-07-09T21:00:00", 2, 1),
    ("England",     "Netherlands", "Semi-Finals", "2024-07-10T21:00:00", 2, 1),

    # ── Final ──────────────────────────────────────────────────────────
    ("Spain",       "England",     "Final", "2024-07-14T21:00:00", 2, 1),
]

# ── Flag codes for Euro 2024 nations ─────────────────────────────────────
EURO_FLAGS = {
    "Germany": "de", "Scotland": "gb-sct", "Hungary": "hu", "Switzerland": "ch",
    "Spain": "es", "Croatia": "hr", "Italy": "it", "Albania": "al",
    "Slovenia": "si", "Denmark": "dk", "Serbia": "rs", "England": "gb-eng",
    "Poland": "pl", "Netherlands": "nl", "Austria": "at", "France": "fr",
    "Belgium": "be", "Slovakia": "sk", "Romania": "ro", "Ukraine": "ua",
    "Turkey": "tr", "Georgia": "ge", "Portugal": "pt", "Czechia": "cz",
}

# ── Fake name parts ────────────────────────────────────────────────────────
FIRST_NAMES = [
    "Alex","Sam","Jordan","Taylor","Morgan","Casey","Riley","Jamie","Avery","Quinn",
    "Drew","Blake","Cameron","Skyler","Peyton","Reese","Parker","Finley","Harper","Emery",
    "Rowan","Hayden","Sawyer","Logan","Dylan","Dakota","River","Sage","Phoenix","Elliot",
    "Charlie","Spencer","Kendall","Oakley","Hunter","Robin","Jesse","Terry","Leslie","Chris",
    "Pat","Lee","Kim","Dana","Rory","Ellis","Frankie","Stevie","Billie","Lane",
    "Marcus","Oliver","Lucas","Noah","Ethan","Mason","Aiden","James","Jack","Henry",
    "Sofia","Emma","Olivia","Ava","Isabella","Mia","Charlotte","Amelia","Harper","Evelyn",
    "Liam","Benjamin","Elijah","William","Mateo","Sebastian","Owen","Wyatt","Julian","Caleb",
    "Zoe","Hannah","Lily","Natalie","Aria","Grace","Chloe","Ella","Nora","Hazel",
    "Finn","Declan","Kieran","Callum","Hamish","Niall","Cian","Ruairi","Tadhg","Padraig",
]
LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Wilson","Taylor",
    "Anderson","Thomas","Jackson","White","Harris","Martin","Thompson","Moore","Young","Allen",
    "King","Wright","Scott","Torres","Nguyen","Hill","Flores","Green","Adams","Nelson",
    "Baker","Hall","Rivera","Campbell","Mitchell","Carter","Roberts","Turner","Phillips","Evans",
    "Edwards","Collins","Stewart","Sanchez","Morris","Rogers","Reed","Cook","Morgan","Bell",
    "Murphy","Bailey","Cooper","Richardson","Cox","Howard","Ward","Peterson","Gray","James",
    "Watson","Brooks","Kelly","Sanders","Price","Bennett","Wood","Barnes","Ross","Henderson",
    "Coleman","Jenkins","Perry","Powell","Long","Patterson","Hughes","Flores","Washington","Butler",
    "Simmons","Foster","Gonzales","Bryant","Alexander","Russell","Griffin","Diaz","Hayes","Myers",
    "Ford","Hamilton","Graham","Sullivan","Wallace","Woods","Cole","West","Jordan","Owens",
]

def random_name():
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


def result_from_scores(hs, as_):
    if hs > as_: return "H"
    if as_ > hs: return "A"
    return "D"


def make_fixture_id(home, away):
    import hashlib
    return str(uuid.UUID(hashlib.md5(f"euro2024-{home}-{away}".encode()).hexdigest()))


def main():
    db.init_db()
    conn = db.get_db()

    # ── Wipe any previous Euro sim data ─────────────────────────────────
    print("Clearing previous Euro 2024 sim data...")
    fixture_ids = [make_fixture_id(h, a) for h, a, *_ in FIXTURES]
    if fixture_ids:
        placeholders = ",".join("?" * len(fixture_ids))
        conn.execute(f"DELETE FROM score_log WHERE pick_id IN (SELECT id FROM picks WHERE fixture_id IN ({placeholders}))", fixture_ids)
        conn.execute(f"DELETE FROM picks WHERE fixture_id IN ({placeholders})", fixture_ids)
        conn.execute(f"DELETE FROM fixtures WHERE id IN ({placeholders})", fixture_ids)

    # Remove existing Euro sim pool
    existing = conn.execute("SELECT id FROM pools WHERE name='Euro 2024 Sim'").fetchone()
    if existing:
        old_pid = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
        conn.execute("DELETE FROM score_log WHERE pool_id=?", (old_pid,))
        conn.execute("DELETE FROM picks WHERE pool_id=?", (old_pid,))
        conn.execute("DELETE FROM pool_members WHERE pool_id=?", (old_pid,))
        conn.execute("DELETE FROM pools WHERE id=?", (old_pid,))

    # Remove sim users from previous runs
    conn.execute("DELETE FROM users WHERE email LIKE 'sim%@euro2024.sim'")
    conn.commit()
    conn.close()

    # ── Insert fixtures ──────────────────────────────────────────────────
    print(f"Inserting {len(FIXTURES)} Euro 2024 fixtures...")
    for home, away, stage, kick_off, hs, as_ in FIXTURES:
        fid = make_fixture_id(home, away)
        db.upsert_fixture({
            "id":             fid,
            "home_team":      home,
            "away_team":      away,
            "home_flag_code": EURO_FLAGS.get(home, "un"),
            "away_flag_code": EURO_FLAGS.get(away, "un"),
            "kick_off":       kick_off,
            "stage":          stage,
        })
        # Set the result immediately
        db.update_fixture_result(fid, hs, as_)

    # ── Create pool ──────────────────────────────────────────────────────
    pool_id = str(uuid.uuid4())
    db.create_pool(pool_id, "Euro 2024 Sim", "Simulated 100-player Euro 2024 picks pool", is_public=1)
    print(f"Created pool: Euro 2024 Sim (id={pool_id})")

    # ── Create 100 sim users and join pool ───────────────────────────────
    print("Creating 100 sim players...")
    user_ids = []
    used_names = set()
    pw_hash = generate_password_hash("simpass123")
    i = 0
    while len(user_ids) < 100:
        fn, ln = random_name()
        key = (fn, ln)
        if key in used_names:
            continue
        used_names.add(key)
        uid = str(uuid.uuid4())
        email = f"sim{i:03d}@euro2024.sim"
        display = f"{fn} {ln}"
        db.create_user(uid, display, email, pw_hash)
        db.join_pool(str(uuid.uuid4()), uid, pool_id)
        user_ids.append(uid)
        i += 1

    # ── Generate picks ───────────────────────────────────────────────────
    # Build correct result map
    correct = {}
    for home, away, stage, kick_off, hs, as_ in FIXTURES:
        fid = make_fixture_id(home, away)
        correct[fid] = result_from_scores(hs, as_)

    print("Generating picks for 100 players across all fixtures...")
    options = ["H", "A", "D"]
    pick_count = 0
    for uid in user_ids:
        # Each player has a "skill" bias: higher skill = more likely to pick correctly
        skill = random.uniform(0.35, 0.70)
        for home, away, stage, kick_off, hs, as_ in FIXTURES:
            fid = make_fixture_id(home, away)
            true_result = correct[fid]
            # With probability=skill, pick correctly; otherwise pick a random wrong option
            if random.random() < skill:
                prediction = true_result
            else:
                wrong = [o for o in options if o != true_result]
                prediction = random.choice(wrong)
            db.upsert_pick(str(uuid.uuid4()), uid, pool_id, fid, prediction)
            pick_count += 1

    print(f"Inserted {pick_count} picks.")

    # ── Score all fixtures ───────────────────────────────────────────────
    print("Scoring all fixtures...")
    total_scored = 0
    for home, away, stage, kick_off, hs, as_ in FIXTURES:
        fid = make_fixture_id(home, away)
        n = db.calculate_scores_for_fixture(fid)
        total_scored += n
    print(f"Scored {total_scored} picks across {len(FIXTURES)} fixtures.")

    # ── Summary ──────────────────────────────────────────────────────────
    conn2 = db.get_db()
    lb = db.get_pool_leaderboard(pool_id)
    print(f"\nTop 10 — Euro 2024 Sim:")
    for rank, row in enumerate(lb[:10], 1):
        print(f"  {rank:2d}. {row['display_name']:<22}  {row['total_points']} pts")
    conn2.close()
    print(f"\nPool ID: {pool_id}")
    print("Done. Log in with any sim user: sim000@euro2024.sim / simpass123")


if __name__ == "__main__":
    main()
