"""
app.py — the main Flask application.

Defines all routes (pages), handles login/logout sessions,
and ties together the database and fixture modules.

To run:
    python3 app.py

Then open http://localhost:5000 in your browser.
A default admin account is created automatically on first run:
    Email:    admin@pool.local
    Password: changeme
"""

import os
import uuid
import functools
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

import database as db
import fixtures as fx

# ── App setup ──────────────────────────────────────────────────────────────

app = Flask(__name__)

# Secret key signs the session cookie. Change this to something random
# and private before deploying publicly.
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")


# ── Auth helpers ───────────────────────────────────────────────────────────

def login_required(f):
    """
    Decorator: redirects to /login if the user isn't logged in.
    
    Usage: put @login_required above any route that needs authentication.
    The original destination is saved so we can redirect back after login.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """
    Decorator: redirects to /home if the logged-in user isn't an admin.
    """
    @functools.wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        user = db.get_user_by_id(session["user_id"])
        if not user or not user["is_admin"]:
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated


def current_user():
    """
    Return the currently logged-in user row, or None.
    
    Also attaches a 'total_score' key for the navbar.
    """
    if "user_id" not in session:
        return None
    user = db.get_user_by_id(session["user_id"])
    if user:
        user = dict(user)
        user["total_score"] = db.get_user_total_score(user["id"])
    return user


# Inject current_user into every template automatically —
# no need to pass it manually to every render_template() call.
@app.context_processor
def inject_user():
    return {"current_user": current_user()}

# Add Python's built-in enumerate to Jinja2 templates
app.jinja_env.globals['enumerate'] = enumerate


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Root URL: redirect to home if logged in, otherwise to login."""
    if "user_id" in session:
        return redirect(url_for("home"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    GET  → show the login form.
    POST → validate credentials and start a session.
    
    On success, redirects to the page the user was trying to reach
    (stored in ?next=), or to /home by default.
    """
    # If already logged in, no need to show login again
    if "user_id" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = db.get_user_by_email(email)

        # check_password_hash compares securely (timing-safe)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            next_page = request.args.get("next", url_for("home"))
            return redirect(next_page)
        else:
            flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    """Clear the session and redirect to login."""
    session.clear()
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email        = request.form.get("email", "").strip().lower()
        password     = request.form.get("password", "")
        if not display_name or not email or len(password) < 8:
            flash("All fields required; password must be at least 8 characters.", "error")
            return render_template("register.html")
        if db.get_user_by_email(email):
            flash("An account with that email already exists.", "error")
            return render_template("register.html")
        user_id = str(uuid.uuid4())
        db.create_user(user_id, display_name, email, generate_password_hash(password))
        session["user_id"] = user_id
        flash(f"Welcome, {display_name}!", "success")
        return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/home")
@login_required
def home():
    """
    Main dashboard page.
    
    Shows:
    - Pools the current user is enrolled in
    - All public pools (with a Join button for ones they haven't joined)
    """
    user = current_user()
    my_pool_ids   = {p["id"] for p in db.get_pools_for_user(user["id"])}
    my_pools      = db.get_pools_for_user(user["id"])
    public_pools  = [p for p in db.get_all_public_pools() if p["id"] not in my_pool_ids]

    return render_template("home.html",
        my_pools=my_pools,
        public_pools=public_pools,
    )


@app.route("/pool/<pool_id>/join", methods=["POST"])
@login_required
def join_pool(pool_id):
    """
    Join a pool by its ID.
    
    Only works for public pools. After joining, redirect to the pool page.
    """
    user = current_user()
    pool = db.get_pool_by_id(pool_id)
    if not pool:
        flash("Pool not found.", "error")
        return redirect(url_for("home"))
    if not pool["is_public"]:
        flash("That pool is not open to the public.", "error")
        return redirect(url_for("home"))

    db.join_pool(str(uuid.uuid4()), user["id"], pool_id)
    flash(f"You joined {pool['name']}!", "success")
    return redirect(url_for("pool_page", pool_id=pool_id))


@app.route("/pool/<pool_id>")
@login_required
def pool_page(pool_id):
    """
    The fixture picker page for a pool.
    
    Shows all fixtures grouped by stage. For each fixture, the user
    can pick Home win (H), Draw (D), or Away win (A).
    Fixtures that have already kicked off are locked (no editing).
    """
    user = current_user()
    pool = db.get_pool_by_id(pool_id)
    if not pool:
        flash("Pool not found.", "error")
        return redirect(url_for("home"))

    # Ensure the user is a member (they could navigate directly to the URL)
    if not db.is_pool_member(user["id"], pool_id):
        flash("You're not a member of that pool.", "error")
        return redirect(url_for("home"))

    # Get all fixtures grouped by stage, refreshing cache if needed
    grouped_fixtures = fx.get_all_fixtures()

    existing_picks = db.get_picks_for_user_in_pool(user["id"], pool_id)

    sc = db.get_db()
    score_rows = sc.execute("""
        SELECT p.fixture_id, sl.points_awarded
        FROM score_log sl JOIN picks p ON p.id = sl.pick_id
        WHERE sl.user_id=? AND sl.pool_id=?
    """, (user["id"], pool_id)).fetchall()

    all_pick_rows = sc.execute("""
        SELECT p.fixture_id, u.display_name, u.email,
               p.predicted_result, sl.points_awarded
        FROM picks p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN score_log sl ON sl.pick_id = p.id
        WHERE p.pool_id=?
        ORDER BY u.display_name
    """, (pool_id,)).fetchall()
    sc.close()

    pick_scores = {r["fixture_id"]: r["points_awarded"] for r in score_rows}
    all_picks_by_fixture = {}
    for r in all_pick_rows:
        all_picks_by_fixture.setdefault(r["fixture_id"], []).append(dict(r))

    leaderboard = db.get_pool_leaderboard(pool_id)
    now_iso = datetime.utcnow().isoformat()

    return render_template("pool.html",
        pool=pool,
        grouped_fixtures=grouped_fixtures,
        existing_picks=existing_picks,
        pick_scores=pick_scores,
        all_picks_by_fixture=all_picks_by_fixture,
        leaderboard=leaderboard,
        now_iso=now_iso,
    )


@app.route("/pool/<pool_id>/pick", methods=["POST"])
@login_required
def submit_pick(pool_id):
    """
    Save a pick via AJAX (called by JavaScript on the pool page).
    
    Expects JSON body: { "fixture_id": "...", "prediction": "H"|"A"|"D" }
    Returns JSON: { "ok": true } or { "ok": false, "error": "..." }
    """
    user = current_user()
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No data sent."}), 400

    fixture_id = data.get("fixture_id", "")
    prediction = data.get("prediction", "")

    if prediction not in ("H", "A", "D"):
        return jsonify({"ok": False, "error": "Invalid prediction."}), 400

    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"ok": False, "error": "Not a pool member."}), 403

    # Check the fixture hasn't kicked off yet
    conn = db.get_db()
    fixture = conn.execute("SELECT kick_off FROM fixtures WHERE id=?", (fixture_id,)).fetchone()
    conn.close()
    if fixture and fixture["kick_off"]:
        if fixture["kick_off"] < datetime.utcnow().isoformat():
            return jsonify({"ok": False, "error": "Fixture has already started."}), 400

    db.upsert_pick(str(uuid.uuid4()), user["id"], pool_id, fixture_id, prediction)
    return jsonify({"ok": True})


@app.route("/admin")
@admin_required
def admin_page():
    config    = db.get_active_scoring_config()
    fixtures  = db.get_fixtures()
    all_pools = db.get_all_public_pools()
    users     = db.get_all_users()

    # Per-pool scoring: {pool_id: {"group_stage": cfg, "knockout": cfg}}
    pool_scoring = {}
    for pool in all_pools:
        pool_scoring[pool["id"]] = {
            "group_stage": db.get_scoring_config(pool["id"], "Group A"),
            "knockout":    db.get_scoring_config(pool["id"], "Round of 16"),
        }

    return render_template("admin.html",
        config=config,
        fixtures=fixtures,
        all_pools=all_pools,
        users=users,
        pool_scoring=pool_scoring,
    )


@app.route("/admin/scoring", methods=["POST"])
@admin_required
def save_scoring():
    user      = current_user()
    pool_id   = request.form.get("pool_id") or None
    round_type = request.form.get("round_type") or None
    try:
        win  = int(request.form["points_win"])
        draw = int(request.form["points_draw"])
        loss = int(request.form["points_loss"])
    except (KeyError, ValueError):
        flash("Please enter valid integer point values.", "error")
        return redirect(url_for("admin_page"))

    db.save_scoring_config(str(uuid.uuid4()), win, draw, loss, user["id"],
                           pool_id=pool_id, round_type=round_type)
    if pool_id:
        pool  = db.get_pool_by_id(pool_id)
        label = f"{pool['name']} — {'Group Stage' if round_type == 'group_stage' else 'Knockout'}"
    else:
        label = "Global default"
    flash(f"Scoring updated ({label}): Win={win}, Draw={draw}, Loss={loss}", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/user/<user_id>/toggle-admin", methods=["POST"])
@admin_required
def toggle_admin(user_id):
    me = current_user()
    if user_id == me["id"]:
        flash("You can't change your own admin status.", "error")
        return redirect(url_for("admin_page"))
    target = db.get_user_by_id(user_id)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("admin_page"))
    new_status = 0 if target["is_admin"] else 1
    db.set_user_admin(user_id, new_status)
    verb = "promoted to" if new_status else "removed from"
    flash(f"{target['display_name']} {verb} admin.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/sync-fixtures", methods=["POST"])
@admin_required
def admin_sync_fixtures():
    db.set_meta("fixtures_last_fetched", "0")
    fx.sync_fixtures()
    flash("Fixtures re-synced.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/fixture/<fixture_id>/result", methods=["POST"])
@admin_required
def set_fixture_result(fixture_id):
    """
    Record a match result and trigger scoring for all picks on that fixture.
    """
    try:
        home_score = int(request.form["home_score"])
        away_score = int(request.form["away_score"])
    except (KeyError, ValueError):
        flash("Please enter valid scores.", "error")
        return redirect(url_for("admin_page"))

    result = db.update_fixture_result(fixture_id, home_score, away_score)
    scored = db.calculate_scores_for_fixture(fixture_id)
    flash(f"Result saved ({result}). Scored {scored} picks.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/pool/create", methods=["POST"])
@admin_required
def create_pool():
    """Create a new pool from the admin page."""
    name        = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    is_public   = 1 if request.form.get("is_public") else 0

    if not name:
        flash("Pool name is required.", "error")
        return redirect(url_for("admin_page"))

    db.create_pool(str(uuid.uuid4()), name, description, is_public)
    flash(f"Pool '{name}' created.", "success")
    return redirect(url_for("admin_page"))


# ── Startup ────────────────────────────────────────────────────────────────

import os

def seed_defaults():
    """
    Create a default admin user and a sample pool if the database is empty.
    
    This runs once on first startup. The admin password should be changed
    immediately in a real deployment.
    """
    # Create admin user if no users exist yet
    conn = db.get_db()
    count = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
    conn.close()

    if count == 0:
        admin_id = str(uuid.uuid4())
        db.create_user(
            user_id      = admin_id,
            display_name = "Admin",
            email        = "admin@pool.local",
            password_hash= generate_password_hash("changeme"),
            is_admin     = 1,
        )
        # Create a sample user too
        db.create_user(
            user_id      = str(uuid.uuid4()),
            display_name = "Alice",
            email        = "alice@example.com",
            password_hash= generate_password_hash("password123"),
        )
        print("[seed] Created admin (admin@pool.local / changeme) and Alice (alice@example.com / password123)")

        # Create a sample public pool
        pool_id = str(uuid.uuid4())
        db.create_pool(pool_id, "The Main Event", "Open picks pool for the 2026 World Cup", is_public=1)
        print(f"[seed] Created pool 'The Main Event' (id={pool_id})")


if __name__ == "__main__":
    db.init_db()
    seed_defaults()
    fx.sync_fixtures()
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, host="0.0.0.0", port=port)
