"""
app.py — the main Flask application.

Defines all routes (pages), handles login/logout sessions,
and ties together the database and fixture modules.

To run:
    python3 app.py

Then open http://localhost:5001 in your browser.
A default admin account is created automatically on first run:
    Email:    admin@pool.local
    Password: changeme
"""

import os
import uuid
import functools
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, g
)
from werkzeug.security import generate_password_hash, check_password_hash

import database as db
import fixtures as fx
import live_now_helper

# ── App setup ──────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")

# Keep users signed in across visits for 90 days (vs the default browser-session
# cookie that vanishes on quit). Set on the session via session.permanent=True
# at login. Matches what most consumer apps do — no SSO needed.
app.permanent_session_lifetime = timedelta(days=90)

# Minutes before kick-off when picks lock
LOCK_MINUTES = 15

# Display kick-off times in this IANA zone. Logic everywhere else stays UTC —
# this only changes what the user sees on the pool/admin pages. zoneinfo
# handles EDT/EST and DST transitions automatically.
DISPLAY_TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "America/New_York"))


def _to_display_iso(utc_iso_str):
    """
    Convert a UTC ISO timestamp ('2026-06-11T18:00:00' or '...Z') to the same
    ISO format in DISPLAY_TZ. Returns the input unchanged if it can't be parsed.
    Used only for what users see — JS countdowns + lock checks keep using
    the raw UTC kick_off so they work consistently across browsers.
    """
    if not utc_iso_str:
        return utc_iso_str
    try:
        s = utc_iso_str.rstrip("Z").rstrip()
        dt = datetime.fromisoformat(s[:19])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return utc_iso_str


def _day_abbrev(utc_iso_str):
    """Return 'Mon'/'Tue'/... in DISPLAY_TZ for a UTC kickoff, or ''."""
    if not utc_iso_str:
        return ""
    try:
        s = utc_iso_str.rstrip("Z").rstrip()
        dt = datetime.fromisoformat(s[:19])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(DISPLAY_TZ).strftime("%a")
    except (ValueError, TypeError):
        return ""


def _attach_display_times(fixtures):
    """Add f['kick_off_display'] and f['kick_off_dow'] to each fixture dict."""
    for f in fixtures:
        f["kick_off_display"] = _to_display_iso(f.get("kick_off"))
        f["kick_off_dow"]     = _day_abbrev(f.get("kick_off"))
    return fixtures


# ── Auth helpers ───────────────────────────────────────────────────────────

def login_required(f):
    """
    Decorator: redirects to /login if the user isn't logged in.
    The original destination is saved so we can redirect back after login.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: redirects to /home if the logged-in user isn't an admin."""
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
    Return the currently logged-in user row, with nav_balance / nav_rank /
    nav_pool_size attached for the sticky header.

    Pool selection priority:
      1. g.nav_pool_id  — set by pool_page so the navbar matches what the
         user is currently looking at
      2. First pool the user has joined (alphabetical) — used for /home,
         /admin, /stats and any other non-pool page
    """
    if "user_id" not in session:
        return None
    user = db.get_user_by_id(session["user_id"])
    if not user:
        return None
    user = dict(user)
    user["total_score"] = db.get_user_total_score(user["id"])

    preferred_id = getattr(g, "nav_pool_id", None)
    pool_for_nav = None
    if preferred_id:
        # User is on a pool page — use that pool for the nav
        pool_for_nav = db.get_pool_by_id(preferred_id)
        if pool_for_nav and db.is_pool_member(user["id"], preferred_id):
            mem = db.get_pool_membership(user["id"], preferred_id)
            user["nav_balance"] = float(mem["balance"] or 0)
            rank, size = db.get_member_rank(user["id"], preferred_id)
            user["nav_rank"] = rank
            user["nav_pool_size"] = size
            user["nav_pool_id"] = preferred_id
            user["nav_pool_name"] = pool_for_nav["name"]
            return user

    pools = db.get_pools_for_user(user["id"])
    if pools:
        first = pools[0]
        user["nav_balance"] = float(first["balance"] or 0)
        rank, size = db.get_member_rank(user["id"], first["id"])
        user["nav_rank"] = rank
        user["nav_pool_size"] = size
        user["nav_pool_id"] = first["id"]
        user["nav_pool_name"] = first["name"]
    else:
        user["nav_balance"] = None
        user["nav_rank"] = None
        user["nav_pool_size"] = None
        user["nav_pool_id"] = None
        user["nav_pool_name"] = None
    return user


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# Country team-name → 3-letter abbreviation lookup. Lives here (vs. each
# template's {% set %}) so both pool.html and group.html — and any future
# templates that render match pills — share one source of truth.
TEAM_ABBR = {
    'United States':'USA','South Korea':'KOR','Saudi Arabia':'KSA',
    'New Zealand':'NZL','South Africa':'RSA','Czech Republic':'CZE',
    'Ivory Coast':'CIV','Costa Rica':'CRC','Cape Verde Islands':'CPV',
    'Bosnia-Herzegovina':'BIH','El Salvador':'ESA','Congo DR':'COD',
    'Curaçao':'CUW','Czechia':'CZE',
    'Argentina':'ARG','Brazil':'BRA','Belgium':'BEL','Mexico':'MEX',
    'Canada':'CAN','England':'ENG','France':'FRA','Germany':'GER',
    'Spain':'ESP','Portugal':'POR','Netherlands':'NED','Italy':'ITA',
    'Croatia':'CRO','Switzerland':'SUI','Denmark':'DEN','Sweden':'SWE',
    'Poland':'POL','Serbia':'SRB','Senegal':'SEN','Morocco':'MAR',
    'Australia':'AUS','Japan':'JPN','Iran':'IRN','Iraq':'IRQ',
    'Algeria':'ALG','Tunisia':'TUN','Cameroon':'CMR','Ghana':'GHA',
    'Nigeria':'NGA','Egypt':'EGY','Jordan':'JOR','Qatar':'QAT',
    'Uruguay':'URU','Colombia':'COL','Ecuador':'ECU','Peru':'PER',
    'Chile':'CHI','Paraguay':'PAR','Bolivia':'BOL','Venezuela':'VEN',
    'Panama':'PAN','Honduras':'HON','Haiti':'HAI',
    'Norway':'NOR','Uzbekistan':'UZB','Scotland':'SCO','Wales':'WAL',
    'Turkey':'TUR','Ukraine':'UKR','Hungary':'HUN','Romania':'ROU',
    'Slovakia':'SVK','Slovenia':'SVN','Austria':'AUT','Georgia':'GEO',
    'Albania':'ALB','Indonesia':'IDN','Mali':'MLI','Zambia':'ZAM',
    'Dominican Republic':'DOM',
}

@app.context_processor
def inject_team_abbr():
    return {"ABBR": TEAM_ABBR}


def _short_name(display_name):
    return (display_name or "").split(" ")[0]


def _disambiguate_short_names(picks):
    """
    Given a list of pick-dicts (each with display_name + email), return
    a dict {email: short_name} that uses the first name when unique and
    adds a last-initial (or further fallback) when two picks share a
    first name. Used by the reveal grid + completed-table tooltips +
    the live banner picks list so duplicate-Greg lists don't confuse.
    """
    by_first = {}
    for p in picks:
        first = _short_name(p.get("display_name"))
        by_first.setdefault(first, []).append(p)
    out = {}
    for first, group in by_first.items():
        if len(group) <= 1:
            email = group[0].get("email") if group else None
            if email:
                out[email] = first
            continue
        # Multiple with the same first name — try last initial.
        for p in group:
            parts = (p.get("display_name") or "").split(" ", 1)
            if len(parts) > 1 and parts[1]:
                out[p["email"]] = f"{first} {parts[1][0].upper()}"
            else:
                # No surname at all — fall back to email prefix.
                out[p["email"]] = (p.get("email") or first).split("@")[0][:10]
        # Catch the unlikely "Greg M" vs "Greg M" case (two Marlons).
        # Re-check uniqueness within the group; for remaining dupes,
        # use the first 8 chars of email.
        seen = {}
        for p in group:
            short = out[p["email"]]
            seen.setdefault(short, []).append(p)
        for short, dupes in seen.items():
            if len(dupes) > 1:
                for p in dupes:
                    out[p["email"]] = (p.get("email") or "").split("@")[0][:10]
    return out


# What's-new releases feed, surfaced as a chip on the pool page + a modal
# listing every release in reverse chronological order.
from releases import RELEASES as _RELEASES, latest_release_date as _latest_rel
@app.context_processor
def inject_releases():
    return {"releases": _RELEASES, "latest_release_date": _latest_rel()}


app.jinja_env.globals['enumerate'] = enumerate


# ── Helpers ────────────────────────────────────────────────────────────────

def _lock_time_for_fixture(kick_off_iso):
    """
    Return the lock datetime (kick_off - LOCK_MINUTES) for a fixture, or None.
    Picks are rejected server-side once now >= lock_time.
    """
    if not kick_off_iso:
        return None
    try:
        ko = datetime.fromisoformat(kick_off_iso[:19])
        return ko - timedelta(minutes=LOCK_MINUTES)
    except (ValueError, TypeError):
        return None


def _is_locked(kick_off_iso):
    """Return True if the pick window for this fixture has closed."""
    lt = _lock_time_for_fixture(kick_off_iso)
    return lt is not None and datetime.utcnow() >= lt


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("home"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """GET → show login form. POST → validate credentials and start session."""
    if "user_id" in session:
        return redirect(url_for("home"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.get_user_by_email(email)
        if user and check_password_hash(user["password_hash"], password):
            session.permanent = True
            session["user_id"] = user["id"]
            db.record_login(user["id"])  # for daily admin digest
            next_page = request.args.get("next", url_for("home"))
            return redirect(next_page)
        else:
            flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """
    Self-service password reset. Form takes an email; if the account
    exists, we mint a magic-login token signed with SECRET_KEY and email
    the deep-link to the user. They click it, land logged in, and can
    change their password from the My Account dialog.

    Security: response is identical whether or not the email exists, so
    this endpoint can't be used to enumerate users.
    """
    if request.method != "POST":
        return render_template("forgot_password.html")

    email = (request.form.get("email", "") or "").strip().lower()
    if not email:
        flash("Email is required.", "error")
        return render_template("forgot_password.html")

    user = db.get_user_by_email(email)
    if user:
        try:
            import magic
            token = magic.make_magic_token(user["id"])
            site_url = os.environ.get(
                "SITE_URL",
                "https://poolboy2-app-production.up.railway.app"
            ).rstrip("/")
            link = f"{site_url}/m/{token}?next=/home"
            _send_reset_email(user, link)
        except Exception as e:
            print(f"[forgot_password] failed to send reset to {email}: {e}")

    # Same flash regardless of whether email exists.
    flash(
        "If an account exists for that email, a sign-in link is on the way. "
        "Check your inbox (and spam folder).",
        "success",
    )
    return redirect(url_for("login"))


def _send_reset_email(user, link):
    """Send a one-click magic-login email styled to match the pool's
    lime/mono aesthetic. `user` may be a sqlite3.Row, a psycopg2 dict
    row, or a plain dict — accept all three by going through dict()."""
    from email_helper import send_email
    user = dict(user)
    display = (user.get("display_name") or "there").split(" ")[0]
    plain = (
        f"Hey {display},\n\n"
        f"Tap the link below to sign in to your WC26 Pool account. The link\n"
        f"is good for 14 days — once you're in, you can rotate your password\n"
        f"from the My Account dialog at the top of the pool page.\n\n"
        f"{link}\n\n"
        f"If you didn't request this, you can safely ignore the email.\n\n"
        f"— WC26 Pool"
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
</head><body style="margin:0;padding:0;background:#f4f4f7;font-family:'JetBrains Mono',Menlo,Consolas,monospace;color:#2a2a35;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="padding:32px 16px;">
  <tr><td align="center">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:520px;background:#fff;border:1px solid #e3e3ec;border-radius:12px;overflow:hidden;">
      <tr><td style="padding:24px 28px 0;">
        <div style="font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#7ea200;font-weight:700;">WC26 Pool</div>
        <h1 style="margin:6px 0 0;font-size:18px;font-weight:700;">Sign in to your account</h1>
      </td></tr>
      <tr><td style="padding:18px 28px 6px;font-size:14px;">Hey {display},</td></tr>
      <tr><td style="padding:0 28px 18px;font-size:13px;color:#6c6c80;line-height:1.55;">
        Tap the button below to sign in. The link is good for 14 days — once
        you're in, you can rotate your password from <strong style="color:#2a2a35">My Account</strong>
        at the top of the pool page.
      </td></tr>
      <tr><td align="center" style="padding:8px 28px 22px;">
        <a href="{link}" style="display:inline-block;padding:13px 32px;background:#d3ff67;color:#2a2a35;font-weight:800;text-decoration:none;border-radius:8px;font-size:14px;letter-spacing:0.02em;">
          Sign in →
        </a>
      </td></tr>
      <tr><td style="padding:0 28px 22px;border-top:1px solid #e3e3ec;font-size:11px;color:#9090a4;text-align:center;letter-spacing:0.04em;">
        Didn't request this? You can safely ignore the email. — WC26 Pool
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""
    send_email(user["email"], "WC26 Pool — sign-in link",
               plain, body_html=html)


@app.route("/m/<token>")
def magic_login(token):
    """One-click login from an email deep link. Verifies the signed token,
    sets the session, and redirects to ?next= (defaults to home)."""
    import magic
    user_id, err = magic.verify_magic_token(token)
    if err == "expired":
        flash("That email link has expired — please log in.", "error")
        return redirect(url_for("login"))
    if err or not user_id:
        flash("Invalid login link.", "error")
        return redirect(url_for("login"))
    user = db.get_user_by_id(user_id)
    if not user:
        return redirect(url_for("login"))
    session.permanent = True
    session["user_id"] = user_id
    db.record_login(user_id)
    nxt = request.args.get("next") or url_for("home")
    # Open-redirect guard: only allow same-site paths
    if not nxt.startswith("/"):
        nxt = url_for("home")
    return redirect(nxt)


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
        session.permanent = True
        session["user_id"] = user_id
        flash(f"Welcome, {display_name}!", "success")
        return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/me/profile", methods=["POST"])
@login_required
def edit_profile():
    """
    Self-service edit of display_name + team_name.
    display_name required, max 30 chars; team_name optional, max 40.
    """
    user = current_user()
    display_name = (request.form.get("display_name", "") or "").strip()
    team_name    = (request.form.get("team_name", "") or "").strip()
    if not display_name:
        flash("First name is required.", "error")
        return redirect(request.referrer or url_for("home"))
    if len(display_name) > 30:
        flash("First name is too long (max 30 chars).", "error")
        return redirect(request.referrer or url_for("home"))
    if len(team_name) > 40:
        flash("Team name is too long (max 40 chars).", "error")
        return redirect(request.referrer or url_for("home"))
    db.update_user_profile(user["id"], display_name, team_name or None)
    flash("Display updated.", "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/me/password", methods=["POST"])
@login_required
def change_password():
    """
    Self-service password change. Requires the current password to prevent
    a hijacked session from silently rotating credentials.
    """
    user = current_user()
    current_pw = request.form.get("current_password", "") or ""
    new_pw     = request.form.get("new_password", "")     or ""
    confirm_pw = request.form.get("confirm_password", "") or ""

    if not check_password_hash(user["password_hash"], current_pw):
        flash("Current password is incorrect.", "error")
        return redirect(request.referrer or url_for("home"))
    if len(new_pw) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(request.referrer or url_for("home"))
    if new_pw != confirm_pw:
        flash("New passwords do not match.", "error")
        return redirect(request.referrer or url_for("home"))

    db.update_user_password(user["id"], generate_password_hash(new_pw))
    flash("Password updated.", "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/admin/user/<user_id>/profile", methods=["POST"])
@admin_required
def admin_edit_profile(user_id):
    """Admin override of any user's display_name + team_name."""
    display_name = (request.form.get("display_name", "") or "").strip()
    team_name    = (request.form.get("team_name", "") or "").strip()
    if not display_name:
        flash("First name is required.", "error")
        return redirect(url_for("admin_page"))
    if len(display_name) > 30 or len(team_name) > 40:
        flash("Name is too long.", "error")
        return redirect(url_for("admin_page"))
    target = db.get_user_by_id(user_id)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("admin_page"))
    db.update_user_profile(user_id, display_name, team_name or None)
    flash(f"Updated profile for {target['email']}.", "success")
    return redirect(url_for("admin_page"))


@app.route("/home")
@login_required
def home():
    user = current_user()
    my_pool_ids  = {p["id"] for p in db.get_pools_for_user(user["id"])}
    my_pools     = db.get_pools_for_user(user["id"])
    public_pools = [p for p in db.get_all_public_pools() if p["id"] not in my_pool_ids]
    return render_template("home.html", my_pools=my_pools, public_pools=public_pools)


@app.route("/pool/<pool_id>/join", methods=["POST"])
@login_required
def join_pool(pool_id):
    user = current_user()
    pool = db.get_pool_by_id(pool_id)
    if not pool:
        flash("Pool not found.", "error")
        return redirect(url_for("home"))
    if not pool["is_public"]:
        flash("That pool is not open to the public.", "error")
        return redirect(url_for("home"))

    has_paid = 0 if pool["entry_fee"] else 1
    # Use the pool's starting_balance from scoring config
    config = db.get_active_scoring_config()
    starting_balance = config.get("starting_balance", 100.0)
    db.join_pool(str(uuid.uuid4()), user["id"], pool_id,
                 has_paid=has_paid, balance=starting_balance)

    if pool["entry_fee"]:
        flash(f"Welcome to {pool['name']}! Don't forget to send your {pool['entry_fee']} entry "
              f"fee — picks are open right away.", "success")
    else:
        flash(f"You joined {pool['name']}!", "success")
    return redirect(url_for("pool_page", pool_id=pool_id))


@app.route("/pool/<pool_id>")
@login_required
def pool_page(pool_id):
    """
    The fixture picker page for a pool.
    Shows all fixtures grouped by stage with economy-based pick/bet/spy UI.
    """
    # Mark which pool's balance/rank should appear in the navbar; current_user()
    # picks this up via Flask's `g` so the chip matches the page the user is on.
    g.nav_pool_id = pool_id
    user = current_user()
    pool = db.get_pool_by_id(pool_id)
    if not pool:
        flash("Pool not found.", "error")
        return redirect(url_for("home"))
    if not db.is_pool_member(user["id"], pool_id):
        flash("You're not a member of that pool.", "error")
        return redirect(url_for("home"))

    grouped_fixtures_raw = fx.get_all_fixtures()
    for stage_fixtures in grouped_fixtures_raw.values():
        _attach_display_times(stage_fixtures)

    # Move every completed fixture into a synthesised "Completed" stage so
    # users can hide/expand the finished games as one group. "Completed" goes
    # first (auto-collapsed) so the upcoming stages stay top-of-page. Within
    # the bucket, sort by kick_off DESC (most recent finish first). Each
    # fixture also gets a short `round` label (MD1/R16/...) stamped on it
    # so the Completed table can subgroup its rows by round.
    _ROUND_SHORT = {
        "Matchday 1": "MD1", "Matchday 2": "MD2", "Matchday 3": "MD3",
        "Round of 32": "R32", "Round of 16": "R16",
        "Quarter-Finals": "QF", "Semi-Finals": "SF",
        "Third Place": "3rd", "Final": "Final",
    }
    # Tournament order — used to keep rounds contiguous in the Completed
    # table even when kickoffs across rounds overlap. Lowest = latest round.
    _ROUND_PRIORITY = {
        "Final": 0, "3rd": 1, "SF": 2, "QF": 3,
        "R16": 4, "R32": 5,
        "MD3": 6, "MD2": 7, "MD1": 8,
    }
    grouped_fixtures = {}
    completed_fixtures = []
    upcoming_by_stage = {}
    for stage_name, fixtures in grouped_fixtures_raw.items():
        short = _ROUND_SHORT.get(stage_name, stage_name)
        for f in fixtures:
            f["round"] = short
        upcoming = [f for f in fixtures if not f.get("result")]
        completed_fixtures.extend(f for f in fixtures if f.get("result"))
        if upcoming:
            upcoming_by_stage[stage_name] = upcoming
    if completed_fixtures:
        # Sort by round (tournament order, latest first), then by kick_off
        # DESC within the round so each round subgroup stays contiguous.
        # Two-pass works because Python's sort is stable.
        completed_fixtures.sort(key=lambda f: f.get("kick_off") or "", reverse=True)
        completed_fixtures.sort(key=lambda f: _ROUND_PRIORITY.get(f.get("round") or "", 99))
        grouped_fixtures["Completed"] = completed_fixtures
    grouped_fixtures.update(upcoming_by_stage)

    existing_picks   = db.get_picks_full_for_user_in_pool(user["id"], pool_id)
    membership       = db.get_pool_membership(user["id"], pool_id)
    has_paid         = bool(membership and membership["has_paid"])
    my_balance       = db.get_member_balance(user["id"], pool_id)
    scoring_config   = db.get_active_scoring_config()
    spy_set          = db.get_spy_set_for_user_in_pool(user["id"], pool_id)
    spy_count        = db.get_spy_count_for_user_in_pool(user["id"], pool_id)

    conn = db.get_db()

    # Payouts from transactions table (economy source of truth)
    payout_rows = conn.execute("""
        SELECT fixture_id, amount FROM transactions
        WHERE user_id=? AND pool_id=? AND type='payout'
    """, (user["id"], pool_id)).fetchall()
    payout_by_fixture = {r["fixture_id"]: r["amount"] for r in payout_rows}

    # All picks in this pool — enriched with user_id and bet_amount
    all_pick_rows = conn.execute("""
        SELECT p.fixture_id, u.id AS user_id, u.display_name, u.email,
               p.predicted_result, p.bet_amount
        FROM picks p
        JOIN users u ON u.id = p.user_id
        WHERE p.pool_id=?
        ORDER BY u.display_name
    """, (pool_id,)).fetchall()
    conn.close()

    # Build {fixture_id: [pick_rows]} and attach visibility flag
    now = datetime.utcnow()

    # Build a flat fixture lookup for lock-time checks
    fixture_lookup = {}
    for stage_fixes in grouped_fixtures.values():
        for f in stage_fixes:
            fixture_lookup[f["id"]] = f

    raw_by_fixture = {}
    for r in all_pick_rows:
        raw_by_fixture.setdefault(r["fixture_id"], []).append(dict(r))

    all_picks_by_fixture = {}
    for fid, picks in raw_by_fixture.items():
        f_data = fixture_lookup.get(fid, {})
        completed = bool(f_data.get("result"))
        locked    = _is_locked(f_data.get("kick_off"))

        # Disambiguate duplicate first names within this fixture's pick set,
        # so reveal tooltips render "Greg M" / "Greg P" instead of two
        # ambiguous "Greg"s.
        shorts = _disambiguate_short_names(picks)

        enriched = []
        for p in picks:
            if p["email"] == user["email"]:
                visible = True
            elif completed or locked:
                visible = True
            elif (fid, p["user_id"]) in spy_set:
                visible = True
            else:
                visible = False
            enriched.append({
                **p,
                "visible":    visible,
                "short_name": shorts.get(p["email"], _short_name(p.get("display_name"))),
            })
        all_picks_by_fixture[fid] = enriched

    group_standings = db.get_group_standings()
    leaderboard     = db.get_pool_leaderboard(pool_id)
    now_iso         = now.isoformat()

    # Aggregate-spy fixture set: which fixtures the user has revealed the
    # field for, so the row can render the spread instead of the buy button.
    aggregate_spy_set = db.get_aggregate_spy_fixture_set(user["id"], pool_id)

    # For each LOCKED-or-COMPLETED fixture (everyone gets the spread free),
    # OR each fixture the user has bought field-spy on, precompute the totals
    # so the template can render the spread inline without a JS fetch.
    # Batched: one query returns per-fixture totals for the whole pool, then
    # we filter to the revealed set. Fixtures with zero picks fall back to a
    # zero-default so the count row still renders 0/0/0.
    all_totals = db.get_fixture_pick_totals_for_pool(pool_id)
    # Derive total_members from the batch result if it has rows; otherwise the
    # pool has no picks yet, so look it up directly.
    if all_totals:
        total_members = next(iter(all_totals.values()))["total_members"]
    else:
        conn = db.get_db()
        row = conn.execute("SELECT COUNT(*) AS n FROM pool_members WHERE pool_id=?", (pool_id,)).fetchone()
        conn.close()
        total_members = row["n"] or 0

    def _empty_totals():
        return {
            "H": {"count": 0, "wagered": 0.0},
            "D": {"count": 0, "wagered": None},
            "A": {"count": 0, "wagered": 0.0},
            "no_pick": total_members,
            "total_members": total_members,
        }

    field_totals = {}
    for fix in fixture_lookup.values():
        fid = fix["id"]
        is_revealed = (
            fid in aggregate_spy_set
            or bool(fix.get("result"))
            or _is_locked(fix.get("kick_off"))
        )
        if is_revealed:
            field_totals[fid] = all_totals.get(fid) or _empty_totals()

    my_rank, pool_size = db.get_member_rank(user["id"], pool_id)
    pick_counts_by_fixture = db.get_pick_counts_for_pool(pool_id)
    top_player_emails = db.get_top_n_player_emails(pool_id, n=5, tie_cap=2)

    # Per-fixture field-spy cost: KO fixtures scale with the pot
    # (max(base, min(pct*pot, cap))); group stage stays flat at base.
    # Batched: one query returns the pot for every fixture this pool has bets on.
    pots = db.get_fixture_pots_for_pool(pool_id)
    field_spy_cost_by_fixture = {}
    _base = float(scoring_config.get("aggregate_spy_cost", 2.0))
    _pct  = float(scoring_config.get("ko_spy_pct", 0.10))
    _cap  = float(scoring_config.get("ko_spy_cap", 20.0))
    for fix in fixture_lookup.values():
        if db.is_knockout_stage(fix.get("stage") or ""):
            pot = pots.get(fix["id"], 0.0)
            field_spy_cost_by_fixture[fix["id"]] = round(max(_base, min(_pct * pot, _cap)), 2)
        else:
            field_spy_cost_by_fixture[fix["id"]] = round(_base, 2)

    # Stages where every fixture has a result render collapsed by default —
    # keeps the page short once a matchday or round is finished. Excludes
    # the synthesised "Completed" stage (already collapsed via its own logic).
    fully_complete_stages = {
        stage_name for stage_name, fixs in grouped_fixtures.items()
        if stage_name != "Completed" and fixs and all(f.get("result") for f in fixs)
    }

    # payouts_by_fixture[fixture_id][user_email] = amount — used to show each
    # revealed player's +$X / -$X on completed fixtures.
    conn = db.get_db()
    payout_rows = conn.execute("""
        SELECT t.fixture_id, u.email, t.amount
        FROM transactions t
        JOIN users u ON u.id = t.user_id
        WHERE t.pool_id=? AND t.type='payout'
    """, (pool_id,)).fetchall()
    conn.close()
    payouts_by_fixture = {}
    for r in payout_rows:
        payouts_by_fixture.setdefault(r["fixture_id"], {})[r["email"]] = float(r["amount"] or 0)

    # Odds-format toggle is only useful for KO fixtures (group stage has
    # no per-team odds rendered). Gate the button until KO stages appear.
    has_ko_stage = any(db.is_knockout_stage(s) for s in grouped_fixtures)

    # Pick accuracy for the top stats tile ("4/14 picks").
    pick_accuracy = db.get_user_pick_accuracy(user["id"], pool_id)

    # Augment every fixture with ESPN's live snapshot (in-memory only —
    # no DB write). Cached 30s so concurrent users share one fetch.
    all_fix_iter = (f for stage_fixtures in grouped_fixtures.values() for f in stage_fixtures)
    live_now_helper.augment_fixtures_with_espn(all_fix_iter)

    # Live-now banner data — any fixture flagged IN_PLAY / PAUSED gets a
    # picks-by-side breakdown so the banner can render bets without
    # another DB hit. Re-used by the /live-now JSON endpoint below.
    live_now_data = _build_live_now_data(pool_id, grouped_fixtures, all_picks_by_fixture)

    return render_template("pool.html",
        pool=pool,
        grouped_fixtures=grouped_fixtures,
        existing_picks=existing_picks,
        payout_by_fixture=payout_by_fixture,
        all_picks_by_fixture=all_picks_by_fixture,
        leaderboard=leaderboard,
        now_iso=now_iso,
        has_paid=has_paid,
        my_balance=my_balance,
        scoring_config=scoring_config,
        spy_count=spy_count,
        spy_set=spy_set,
        aggregate_spy_set=aggregate_spy_set,
        field_totals=field_totals,
        pick_counts_by_fixture=pick_counts_by_fixture,
        top_player_emails=top_player_emails,
        payouts_by_fixture=payouts_by_fixture,
        fully_complete_stages=fully_complete_stages,
        field_spy_cost_by_fixture=field_spy_cost_by_fixture,
        group_standings=group_standings,
        lock_minutes=LOCK_MINUTES,
        my_rank=my_rank,
        pool_size=pool_size,
        has_ko_stage=has_ko_stage,
        live_now=live_now_data,
        pick_accuracy=pick_accuracy,
    )


def _build_live_now_data(pool_id, grouped_fixtures, all_picks_by_fixture):
    """
    Per-fixture data for the Live banner. Includes any fixture that's
    either (a) flagged IN_PLAY / PAUSED by the auto-score cron, OR (b)
    locked — i.e. kick_off is within LOCK_MINUTES of now, but not yet
    completed. The "locked" window means the banner opens up to 15 min
    before kickoff so users get the deep view in the build-up too.

    Skips completed fixtures (those that already settled).

    Each entry: {id, home_team, away_team, *_flag, live_*, stage,
                 minute_est, picks_by_side, is_ko, is_pre_kick}
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    LIVE = {"IN_PLAY", "PAUSED"}
    now = _dt.now(_tz.utc)
    out = []
    for stage_fixtures in grouped_fixtures.values():
        for f in stage_fixtures:
            if f.get("result"):                      # already settled
                continue
            live_status = (f.get("live_status") or "")
            is_live = live_status in LIVE

            ko = f.get("kick_off") or ""
            ko_dt = None
            try:
                ko_dt = _dt.fromisoformat(ko[:19].rstrip("Z"))
                if ko_dt.tzinfo is None:
                    ko_dt = ko_dt.replace(tzinfo=_tz.utc)
            except Exception:
                pass

            is_pre_kick = False
            if not is_live:
                # Not flagged live by the API yet — check the lock window.
                # The banner opens once lock fires (kick_off - LOCK_MINUTES).
                if ko_dt is None:
                    continue
                lock_dt = ko_dt - _td(minutes=LOCK_MINUTES)
                if not (lock_dt <= now < ko_dt + _td(hours=4)):
                    continue
                is_pre_kick = now < ko_dt
            # Prefer the real minute that ESPN supplies; fall back to
            # kick-off-derived estimate when no ESPN data is available.
            minute_est = f.get("live_minute")
            if minute_est is None and ko_dt is not None and now >= ko_dt:
                delta_min = int((now - ko_dt).total_seconds() // 60)
                if 0 <= delta_min <= 120:
                    minute_est = delta_min

            picks = all_picks_by_fixture.get(f["id"], [])
            # Disambiguate first names across this fixture's pick set so the
            # banner doesn't show two ambiguous "Greg"s.
            shorts = _disambiguate_short_names(picks)
            picks_by_side = {"H": [], "D": [], "A": []}
            for p in picks:
                side = p.get("predicted_result")
                if side in picks_by_side:
                    picks_by_side[side].append({
                        "user_id":      p.get("user_id"),
                        "email":        p.get("email"),
                        "first_name":   shorts.get(p.get("email"),
                                                   _short_name(p.get("display_name"))),
                        "bet_amount":   p.get("bet_amount"),
                    })
            for side in picks_by_side:
                picks_by_side[side].sort(
                    key=lambda x: (-(x.get("bet_amount") or 0), x["first_name"].lower())
                )
            out.append({
                "id":            f["id"],
                "home_team":     f.get("home_team"),
                "away_team":     f.get("away_team"),
                "home_flag":     f.get("home_flag_code") or "un",
                "away_flag":     f.get("away_flag_code") or "un",
                "live_home":     f.get("live_home_score"),
                "live_away":     f.get("live_away_score"),
                "live_status":   f.get("live_status"),
                "stage":         f.get("stage"),
                "city":          f.get("city"),
                "minute_est":    minute_est,
                "picks_by_side": picks_by_side,
                "is_ko":         db.is_knockout_stage(f.get("stage") or ""),
                "is_pre_kick":   is_pre_kick,
            })
    return out


@app.route("/pool/<pool_id>/live-now")
@login_required
def live_now_json(pool_id):
    """JSON endpoint the live-banner JS polls every 30s. Same shape as
    the server-rendered `live_now` template var."""
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"error": "not a member"}), 403

    # Re-run a minimal slice of the pool_page data prep — just enough to
    # rebuild live_now_data without all the leaderboard / spy work.
    grouped_raw = fx.get_all_fixtures()
    for stage_fixtures in grouped_raw.values():
        _attach_display_times(stage_fixtures)

    # ESPN augmentation (same cached fetch as the page render).
    all_fix_iter = (f for sf in grouped_raw.values() for f in sf)
    live_now_helper.augment_fixtures_with_espn(all_fix_iter)

    conn = db.get_db()
    pick_rows = conn.execute("""
        SELECT p.fixture_id, u.id AS user_id, u.display_name, u.email,
               p.predicted_result, p.bet_amount
        FROM picks p JOIN users u ON u.id = p.user_id
        WHERE p.pool_id=?
    """, (pool_id,)).fetchall()
    conn.close()
    all_picks_by_fixture = {}
    for r in pick_rows:
        all_picks_by_fixture.setdefault(r["fixture_id"], []).append(dict(r))

    live = _build_live_now_data(pool_id, grouped_raw, all_picks_by_fixture)
    return jsonify({"live": live, "my_email": user["email"]})


# ── Live-match chat ─────────────────────────────────────────────────────

CHAT_MAX_LEN = 280


def _chat_message_json(row):
    """Shape a chat row for the wire. `score` is a number used to compute
    font-size only — never rendered as a count. `my_vote` is the viewer's
    own thumb state (-1/0/+1)."""
    r = dict(row)
    return {
        "id":           r.get("id"),
        "user_id":      r.get("user_id"),
        "display_name": r.get("display_name"),
        "team_name":    r.get("team_name"),
        "email":        r.get("email"),
        "body":         r.get("body"),
        "created_at":   r.get("created_at"),
        "minute":       r.get("posted_minute"),
        "pick":         r.get("predicted_result"),
        "bet_amount":   r.get("bet_amount"),
        "score":        int(r.get("score") or 0),
        "my_vote":      int(r.get("my_vote") or 0),
    }


def _fixture_live_state(fixture_id):
    """Return (is_live, live_minute). Chat is considered live whenever the
    live-now banner would show the fixture: ESPN-flagged IN_PLAY/PAUSED,
    OR inside the pre-kickoff lock window (LOCK_MINUTES before kickoff)
    through 4 h after kickoff. Mirrors `_build_live_now_data`'s gate so
    users can chat in the build-up too."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    conn = db.get_db()
    row = conn.execute(
        "SELECT live_status, live_minute, kick_off FROM fixtures WHERE id=?",
        (fixture_id,)
    ).fetchone()
    conn.close()
    if not row:
        return False, None
    r = dict(row)
    if (r.get("live_status") or "") in ("IN_PLAY", "PAUSED"):
        return True, r.get("live_minute")
    ko = r.get("kick_off") or ""
    try:
        ko_dt = _dt.fromisoformat(ko[:19].rstrip("Z"))
        if ko_dt.tzinfo is None:
            ko_dt = ko_dt.replace(tzinfo=_tz.utc)
    except Exception:
        return False, None
    now = _dt.now(_tz.utc)
    if (ko_dt - _td(minutes=LOCK_MINUTES)) <= now < ko_dt + _td(hours=4):
        # Pre-kickoff: no minute. After kickoff with no ESPN flag yet:
        # use the kickoff-derived estimate (matches live-now's fallback).
        if now < ko_dt:
            return True, None
        delta_min = int((now - ko_dt).total_seconds() // 60)
        return True, max(0, min(120, delta_min))
    return False, None


def _fixture_is_live(fixture_id):
    return _fixture_live_state(fixture_id)[0]


@app.route("/pool/<pool_id>/chat/<fixture_id>", methods=["POST"])
@login_required
def post_chat_message(pool_id, fixture_id):
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"error": "not a member"}), 403
    if db.is_user_chat_banned(user["id"]):
        return jsonify({"error": "chat banned"}), 403
    is_live, live_min = _fixture_live_state(fixture_id)
    if not is_live:
        return jsonify({"error": "match not live"}), 403

    data = request.get_json(silent=True) or {}
    body = (data.get("message") or "").strip()
    if not body:
        return jsonify({"error": "empty message"}), 400
    if len(body) > CHAT_MAX_LEN:
        return jsonify({"error": f"max {CHAT_MAX_LEN} chars"}), 400

    msg_id = str(uuid.uuid4())
    row = db.add_chat_message(msg_id, user["id"], pool_id, fixture_id, body,
                              posted_minute=live_min)
    return jsonify({"message": _chat_message_json(row)}), 201


@app.route("/pool/<pool_id>/chat/<fixture_id>")
@login_required
def get_chat_messages(pool_id, fixture_id):
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"error": "not a member"}), 403
    since = request.args.get("since") or None
    rows = db.get_chat_messages_since(pool_id, fixture_id, since_iso=since,
                                      limit=100, viewer_id=user["id"])
    # Always return the current vote state for the last N messages so the
    # client can keep already-rendered messages' size + button state in
    # sync when other users vote between polls.
    votes = db.get_chat_vote_states(pool_id, fixture_id, user["id"], limit=200)
    return jsonify({
        "messages":     [_chat_message_json(r) for r in rows],
        "votes":        votes,
        "my_email":     user["email"],
        "fixture_live": _fixture_is_live(fixture_id),
    })


@app.route("/pool/<pool_id>/chat/<fixture_id>/vote/<message_id>", methods=["POST"])
@login_required
def vote_chat_message(pool_id, fixture_id, message_id):
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"error": "not a member"}), 403
    if db.is_user_chat_banned(user["id"]):
        return jsonify({"error": "chat banned"}), 403
    data = request.get_json(silent=True) or {}
    try:
        vote = int(data.get("vote", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "bad vote"}), 400
    if vote not in (-1, 0, 1):
        return jsonify({"error": "bad vote"}), 400
    msg = db.get_chat_message(message_id)
    if not msg:
        return jsonify({"error": "no such message"}), 404
    if msg["user_id"] == user["id"]:
        return jsonify({"error": "cannot vote on own message"}), 403
    result = db.cast_chat_vote(message_id, user["id"], vote)
    return jsonify(result)


@app.route("/pool/<pool_id>/chat/<message_id>", methods=["DELETE"])
@login_required
def delete_chat_message(pool_id, message_id):
    """Inline delete. The original author can always remove their own
    message; admins can remove any message in any pool they can see."""
    user = current_user()
    is_admin = bool(user.get("is_admin"))
    if not is_admin and not db.is_pool_member(user["id"], pool_id):
        return jsonify({"error": "not a member"}), 403
    msg = db.get_chat_message(message_id)
    if not msg:
        return jsonify({"error": "not found"}), 404
    if msg["user_id"] != user["id"] and not is_admin:
        return jsonify({"error": "forbidden"}), 403
    db.soft_delete_chat_message(message_id)
    return jsonify({"ok": True})


@app.route("/admin/chat/<message_id>", methods=["DELETE"])
@admin_required
def admin_delete_chat(message_id):
    db.soft_delete_chat_message(message_id)
    return jsonify({"ok": True})


@app.route("/admin/user/<user_id>/chat-ban", methods=["POST"])
@admin_required
def admin_toggle_chat_ban(user_id):
    """Toggle chat ban. Body: {banned: true|false}."""
    data = request.get_json(silent=True) or {}
    banned = bool(data.get("banned"))
    db.set_user_chat_banned(user_id, banned)
    return jsonify({"ok": True, "banned": banned})


@app.route("/pool/<pool_id>/group/<letter>")
@login_required
def group_page(pool_id, letter):
    """
    Focused per-group view: all six fixtures in Group <letter> in kick_off
    order, with the standings table at top + inline pick UI. Picks POST to
    the same /pool/<id>/pick endpoint, so no separate save logic is needed.
    """
    g.nav_pool_id = pool_id
    user = current_user()
    pool = db.get_pool_by_id(pool_id)
    if not pool:
        flash("Pool not found.", "error"); return redirect(url_for("home"))
    if not db.is_pool_member(user["id"], pool_id):
        flash("You're not a member of that pool.", "error"); return redirect(url_for("home"))

    letter = (letter or "").upper()
    if len(letter) != 1 or not letter.isalpha():
        flash("Unknown group.", "error"); return redirect(url_for("pool_page", pool_id=pool_id))
    stage = f"Group {letter}"

    # Fetch group fixtures directly — get_all_fixtures() rebuckets group
    # fixtures into Matchday 1/2/3 and loses the Group letter identity.
    conn = db.get_db()
    fixture_rows = conn.execute(
        "SELECT * FROM fixtures WHERE stage=? ORDER BY kick_off", (stage,)
    ).fetchall()
    conn.close()
    fixtures = [dict(r) for r in fixture_rows]
    if not fixtures:
        flash(f"No fixtures found for {stage}.", "error")
        return redirect(url_for("pool_page", pool_id=pool_id))

    _attach_display_times(fixtures)

    existing_picks = db.get_picks_full_for_user_in_pool(user["id"], pool_id)
    membership     = db.get_pool_membership(user["id"], pool_id)
    has_paid       = bool(membership and membership["has_paid"])
    my_balance     = db.get_member_balance(user["id"], pool_id)
    scoring_config = db.get_active_scoring_config()
    group_standings = db.get_group_standings()
    teams = group_standings.get(stage, [])

    # Per-fixture payouts for the user (same source the main pool page reads
    # for the "+$5 / $0" badge under completed pills).
    payout_conn = db.get_db()
    payout_rows = payout_conn.execute("""
        SELECT fixture_id, amount FROM transactions
        WHERE user_id=? AND pool_id=? AND type='payout'
    """, (user["id"], pool_id)).fetchall()
    payout_conn.close()
    payout_by_fixture = {r["fixture_id"]: r["amount"] for r in payout_rows}

    return render_template("group.html",
        pool=pool,
        stage=stage,
        group_letter=letter,
        fixtures=fixtures,
        teams=teams,
        existing_picks=existing_picks,
        payout_by_fixture=payout_by_fixture,
        has_paid=has_paid,
        my_balance=my_balance,
        scoring_config=scoring_config,
        now_iso=datetime.now(timezone.utc).isoformat(),
        lock_minutes=LOCK_MINUTES,
    )


def _render_combined_stats(pool_id):
    """Shared renderer for /leaderboard and /stats. Builds the leaderboard
    with enrichment columns (correct%, rarest pick, current streak) plus
    the per-match balance chart data."""
    g.nav_pool_id = pool_id
    user = current_user()
    pool = db.get_pool_by_id(pool_id)
    if not pool:
        flash("Pool not found.", "error")
        return redirect(url_for("home"))
    if not db.is_pool_member(user["id"], pool_id):
        flash("You're not a member of that pool.", "error")
        return redirect(url_for("home"))

    leaderboard = db.get_pool_leaderboard(pool_id)
    # Enrich every leaderboard row with the new stats columns. Three
    # lightweight queries per user — fine at the pool sizes we expect (50).
    enriched = []
    for row in leaderboard:
        uid = row["user_id"]
        acc    = db.get_user_pick_accuracy(uid, pool_id)
        streak = db.get_user_current_streak(uid, pool_id)
        rare   = db.get_user_rarest_pick(uid, pool_id)
        pct    = round((acc["correct"] * 100 / acc["total"]), 0) if acc["total"] else None
        enriched.append({
            **dict(row),
            "correct":   acc["correct"],
            "total":     acc["total"],
            "pct":       pct,
            "streak":    streak,
            "rare_pick": rare,
        })

    # Balance per scored match (one column per match).
    balance_timeline = db.get_pool_balance_per_match(pool_id, max_players=10_000)
    # If user not in top of leaderboard's first 20 chart slice, ensure their
    # series is appended so they always see themselves on the chart.
    my_uid = user["id"]
    chart_players = balance_timeline["players"][:20]
    if not any(p["user_id"] == my_uid for p in chart_players):
        for p in balance_timeline["players"]:
            if p["user_id"] == my_uid:
                chart_players = chart_players + [p]
                break
    balance_timeline["players"] = chart_players

    return render_template("stats_combined.html",
        pool=pool,
        leaderboard=enriched,
        my_balance=db.get_member_balance(user["id"], pool_id),
        my_user_id=my_uid,
        timeline=balance_timeline,
        TEAM_ABBR=TEAM_ABBR,
    )


@app.route("/pool/<pool_id>/leaderboard")
@login_required
def leaderboard_page(pool_id):
    """Now an alias for the combined Stats page (kept for back-compat)."""
    return _render_combined_stats(pool_id)


@app.route("/pool/<pool_id>/pick", methods=["POST"])
@login_required
def submit_pick(pool_id):
    """
    Save a pick via AJAX.

    For group fixtures: JSON {fixture_id, prediction}
    For knockout fixtures: JSON {fixture_id, prediction, bet_amount}

    Picks lock LOCK_MINUTES before kick-off. Knockout bets are deducted
    immediately from the member's balance; changing a KO pick before lock
    refunds the old bet and deducts the new one.

    Returns JSON {ok, error?, new_balance?}
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

    # Payment is tracked in pool_members.has_paid for admin bookkeeping but
    # not enforced here — players can pick immediately on join. The admin
    # reconciles e-transfers manually via /admin.

    # Fetch fixture for lock-time and stage checks
    conn = db.get_db()
    fixture = conn.execute(
        "SELECT kick_off, stage FROM fixtures WHERE id=?", (fixture_id,)
    ).fetchone()

    # Get existing pick for KO refund calculation
    existing_pick = conn.execute(
        "SELECT predicted_result, bet_amount FROM picks WHERE user_id=? AND pool_id=? AND fixture_id=?",
        (user["id"], pool_id, fixture_id)
    ).fetchone()
    conn.close()

    # Enforce lock time (15 min before kick-off)
    if fixture and fixture["kick_off"]:
        if _is_locked(fixture["kick_off"]):
            return jsonify({"ok": False, "error": "Pick window has closed (15 min before kick-off)."}), 400

    knockout = db.is_knockout_stage(fixture["stage"] if fixture else "")

    if knockout and prediction == "D":
        return jsonify({"ok": False, "error": "Draws are not allowed in knockout rounds."}), 400

    if knockout:
        bet_raw = data.get("bet_amount")
        try:
            bet_amount = float(bet_raw) if bet_raw is not None else None
        except (TypeError, ValueError):
            bet_amount = None

        if bet_amount is None or bet_amount <= 0:
            return jsonify({"ok": False, "error": "A positive bet amount is required for knockout picks."}), 400

        balance = db.get_member_balance(user["id"], pool_id)
        old_bet = (existing_pick["bet_amount"] or 0.0) if existing_pick else 0.0
        # Effective cost = new bet minus refund of existing bet
        net_cost = bet_amount - old_bet
        if net_cost > balance:
            return jsonify({"ok": False,
                            "error": f"Insufficient balance. You have ${balance:.2f} available."}), 400

        # Atomically: refund old bet (if any), deduct new bet, update pick
        _apply_ko_bet(user["id"], pool_id, fixture_id, old_bet, bet_amount)
    else:
        bet_amount = None

    db.upsert_pick(str(uuid.uuid4()), user["id"], pool_id, fixture_id, prediction, bet_amount)
    new_balance = db.get_member_balance(user["id"], pool_id)
    return jsonify({"ok": True, "new_balance": new_balance})


def _apply_ko_bet(user_id, pool_id, fixture_id, old_bet, new_bet):
    """
    Refund old_bet (if > 0) and deduct new_bet for a knockout pick change.
    Writes bet transactions and updates pool_members.balance.
    All in one DB round-trip for consistency.
    """
    conn = db.get_db()
    if old_bet > 0:
        db._write_transaction(conn, str(uuid.uuid4()), user_id, pool_id, fixture_id,
                              "adjustment", old_bet, f"KO bet refund (pick changed)")
        db._update_balance(conn, user_id, pool_id, old_bet)
    db._write_transaction(conn, str(uuid.uuid4()), user_id, pool_id, fixture_id,
                          "bet", -new_bet, f"KO bet placed · ${new_bet:.2f}")
    db._update_balance(conn, user_id, pool_id, -new_bet)
    conn.commit()
    conn.close()


@app.route("/pool/<pool_id>/fixture/<fixture_id>/picks")
@login_required
def fixture_picks(pool_id, fixture_id):
    """
    Return all picks for a fixture in a pool — only accessible after the lock time.

    Called by the countdown timer when it reaches zero to reveal all picks in-place
    without a page reload. Returns 403 if the lock time hasn't passed yet.

    Response JSON:
      {ok, is_knockout, picks: [{email, display_name, user_id, predicted_result, bet_amount}]}
    """
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"ok": False, "error": "Not a pool member."}), 403

    conn = db.get_db()
    fixture = conn.execute(
        "SELECT kick_off, stage FROM fixtures WHERE id=?", (fixture_id,)
    ).fetchone()

    if not fixture:
        conn.close()
        return jsonify({"ok": False, "error": "Fixture not found."}), 404

    if not _is_locked(fixture["kick_off"]):
        conn.close()
        return jsonify({"ok": False, "error": "Picks not yet visible."}), 403

    pick_rows = conn.execute("""
        SELECT u.id AS user_id, u.email, u.display_name,
               p.predicted_result, p.bet_amount
        FROM picks p
        JOIN users u ON u.id = p.user_id
        WHERE p.pool_id=? AND p.fixture_id=?
        ORDER BY u.display_name
    """, (pool_id, fixture_id)).fetchall()
    conn.close()

    return jsonify({
        "ok": True,
        "is_knockout": db.is_knockout_stage(fixture["stage"]),
        "picks": [dict(r) for r in pick_rows],
    })


@app.route("/pool/<pool_id>/fixture/<fixture_id>/field-spy", methods=["POST"])
@login_required
def field_spy(pool_id, fixture_id):
    """
    Purchase or refresh the field-spy: H/D/A vote distribution for one fixture.

    First purchase deducts aggregate_spy_cost. Subsequent calls (same buyer,
    same fixture) return the current spread free of charge — so the player
    can re-check until kick-off without paying again.

    Once a fixture has been kicked-off (passes lock time) or has a recorded
    result, the spread is free for everyone in the pool.
    """
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"ok": False, "error": "Not a pool member."}), 403

    conn = db.get_db()
    fixture = conn.execute(
        "SELECT kick_off, stage, result FROM fixtures WHERE id=?", (fixture_id,)
    ).fetchone()
    conn.close()
    if not fixture:
        return jsonify({"ok": False, "error": "Fixture not found."}), 404

    knockout = db.is_knockout_stage(fixture["stage"] or "")
    locked_or_done = _is_locked(fixture["kick_off"]) or fixture["result"]
    already_bought = db.has_bought_aggregate_spy(user["id"], pool_id, fixture_id)
    config = db.get_active_scoring_config()
    base_cost = float(config.get("aggregate_spy_cost", 2.0))

    # Dynamic field-spy pricing on knockouts:
    #   max( base_cost, min( ko_spy_pct * pot, ko_spy_cap ) )
    # Group stage stays at the flat base cost — pots are small, banter > revenue.
    pricing_explain = None
    if knockout:
        pot = db.get_fixture_pot(pool_id, fixture_id)
        pct = float(config.get("ko_spy_pct", 0.10))
        cap = float(config.get("ko_spy_cap", 20.0))
        dynamic = min(pct * pot, cap)
        cost = round(max(base_cost, dynamic), 2)
        pricing_explain = {
            "model": "ko_dynamic",
            "pot": round(pot, 2),
            "pct": pct,
            "cap": cap,
            "base": base_cost,
        }
    else:
        cost = round(base_cost, 2)
        pricing_explain = {"model": "flat", "base": base_cost}

    if already_bought or locked_or_done:
        # Free path — return spread, don't charge
        totals = db.get_fixture_pick_totals(pool_id, fixture_id, knockout=knockout)
        return jsonify({"ok": True, "free": True,
                        "new_balance": db.get_member_balance(user["id"], pool_id),
                        "totals": totals, "is_knockout": knockout,
                        "pricing": pricing_explain})

    balance = db.get_member_balance(user["id"], pool_id)
    if balance < cost:
        return jsonify({"ok": False, "error": f"Insufficient balance. Need ${cost:.2f}."}), 400

    charged = db.record_aggregate_spy(
        spy_id=str(uuid.uuid4()), tx_id=str(uuid.uuid4()),
        user_id=user["id"], pool_id=pool_id, fixture_id=fixture_id, cost=cost
    )
    totals = db.get_fixture_pick_totals(pool_id, fixture_id, knockout=knockout)
    return jsonify({
        "ok": True, "free": (not charged), "cost": cost,
        "new_balance": db.get_member_balance(user["id"], pool_id),
        "totals": totals, "is_knockout": knockout,
        "pricing": pricing_explain,
    })


@app.route("/pool/<pool_id>/fixture/<fixture_id>/spy-list")
@login_required
def spy_list(pool_id, fixture_id):
    """Return other pool members for the 'Spy on Someone' modal."""
    user = current_user()
    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"ok": False, "error": "Not a pool member."}), 403

    members = db.get_pool_members_for_spy_list(user["id"], pool_id, fixture_id)
    spy_count = db.get_spy_count_for_user_in_pool(user["id"], pool_id)
    config = db.get_active_scoring_config()
    next_cost = round(config["spy_base_cost"] + config["spy_increment"] * spy_count, 2)
    return jsonify({
        "ok": True,
        "next_spy_cost": next_cost,
        "my_balance": db.get_member_balance(user["id"], pool_id),
        "members": [
            {"display_name": m["display_name"], "email": m["email"],
             "balance": round(m["balance"], 2), "already_spied": bool(m["already_spied"])}
            for m in members
        ],
    })


@app.route("/pool/<pool_id>/spy", methods=["POST"])
@login_required
def spy_pick(pool_id):
    """
    Purchase spy access to reveal one competitor's pick for one fixture.

    Request JSON: {fixture_id, target_email}
    Response JSON: {ok, pick: {predicted_result, bet_amount}, new_balance, next_spy_cost, error?}

    Cost = spy_base_cost + spy_increment × total_spies_purchased_so_far.
    If the fixture is already past lock time, picks are free (returns pick with no charge).
    If the user has already paid to spy on this target+fixture, returns pick for free.
    """
    user = current_user()
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No data."}), 400

    fixture_id   = data.get("fixture_id", "")
    target_email = data.get("target_email", "")

    if not db.is_pool_member(user["id"], pool_id):
        return jsonify({"ok": False, "error": "Not a pool member."}), 403

    # Spies (like picks) are not payment-gated — admin reconciles entry fees
    # via the has_paid flag in /admin, but the site doesn't block playing.

    target = db.get_user_by_email(target_email)
    if not target:
        return jsonify({"ok": False, "error": "Target user not found."}), 404

    if target["id"] == user["id"]:
        return jsonify({"ok": False, "error": "Cannot spy on yourself."}), 400

    conn = db.get_db()
    fixture = conn.execute("SELECT kick_off, stage FROM fixtures WHERE id=?", (fixture_id,)).fetchone()
    if not fixture:
        conn.close()
        return jsonify({"ok": False, "error": "Fixture not found."}), 404

    # After lock time: picks are free — return without charging
    if _is_locked(fixture["kick_off"]):
        pick = conn.execute(
            "SELECT predicted_result, bet_amount FROM picks WHERE user_id=? AND pool_id=? AND fixture_id=?",
            (target["id"], pool_id, fixture_id)
        ).fetchone()
        conn.close()
        config = db.get_active_scoring_config()
        spy_count = db.get_spy_count_for_user_in_pool(user["id"], pool_id)
        return jsonify({
            "ok": True,
            "pick": dict(pick) if pick else None,
            "free": True,
            "new_balance": db.get_member_balance(user["id"], pool_id),
            "next_spy_cost": round(config["spy_base_cost"] + config["spy_increment"] * spy_count, 2),
            "is_knockout": db.is_knockout_stage(fixture["stage"]),
        })
    conn.close()

    # Compute cost
    config    = db.get_active_scoring_config()
    spy_count = db.get_spy_count_for_user_in_pool(user["id"], pool_id)
    cost      = round(config["spy_base_cost"] + config["spy_increment"] * spy_count, 2)
    balance   = db.get_member_balance(user["id"], pool_id)

    # Idempotency: if this buyer already spied on this target+fixture, return the
    # pick for free without re-charging. Distinct from a true new purchase below.
    already_spied = db.has_spied(user["id"], target["id"], pool_id, fixture_id)
    if already_spied:
        pick = db.get_pick_for_user_fixture(target["id"], pool_id, fixture_id)
        return jsonify({
            "ok": True,
            "pick": pick,
            "free": True,
            "new_balance": balance,
            "next_spy_cost": cost,
            "is_knockout": db.is_knockout_stage(fixture["stage"] or ""),
        })

    if balance < cost:
        return jsonify({"ok": False, "error": f"Insufficient balance. Need ${cost:.2f}."}), 400

    pick = db.record_spy(
        spy_id     = str(uuid.uuid4()),
        tx_id      = str(uuid.uuid4()),
        buyer_id   = user["id"],
        target_id  = target["id"],
        pool_id    = pool_id,
        fixture_id = fixture_id,
        cost       = cost,
    )

    new_balance  = db.get_member_balance(user["id"], pool_id)
    new_spy_count = spy_count + 1
    next_cost = round(config["spy_base_cost"] + config["spy_increment"] * new_spy_count, 2)

    return jsonify({
        "ok": True,
        "pick": pick,
        "cost": cost,
        "new_balance": new_balance,
        "next_spy_cost": next_cost,
        "is_knockout": db.is_knockout_stage(fixture["stage"] or ""),
    })


# ── Admin routes ────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_page():
    config    = db.get_active_scoring_config()
    fixtures  = [dict(f) for f in db.get_fixtures()]
    _attach_display_times(fixtures)
    all_pools = db.get_all_public_pools()
    users     = db.get_all_users()

    pool_scoring = {}
    pool_members = {}
    pool_balances = {}
    pool_transactions = {}

    for pool in all_pools:
        pid = pool["id"]
        pool_scoring[pid] = {
            "group_stage": db.get_scoring_config(pid, "Group A"),
            "knockout":    db.get_scoring_config(pid, "Round of 16"),
        }
        pool_members[pid]      = db.get_pool_members(pid)
        pool_balances[pid]     = db.get_pool_members_with_balances(pid)
        pool_transactions[pid] = {
            m["id"]: db.get_user_transactions(m["id"], pid)
            for m in pool_balances[pid]
        }

    # Build per-user memberships-with-balance dict for the user-management
    # dropdown panel. Each user gets a list of their pool memberships with
    # has_paid + balance + pool_name surfaced.
    user_memberships = {}
    for u in users:
        uid = u["id"]
        m_for_user = []
        for pool in all_pools:
            pid = pool["id"]
            mem = next((m for m in pool_balances[pid] if m["id"] == uid), None)
            if mem:
                m_for_user.append({
                    "pool_id":    pid,
                    "pool_name":  pool["name"],
                    "has_paid":   bool(mem.get("has_paid")),
                    "balance":    float(mem.get("balance") or 0),
                })
        user_memberships[uid] = m_for_user

    return render_template("admin.html",
        config=config,
        fixtures=fixtures,
        all_pools=all_pools,
        users=users,
        user_memberships=user_memberships,
        pool_scoring=pool_scoring,
        pool_members=pool_members,
        pool_balances=pool_balances,
        pool_transactions=pool_transactions,
    )


@app.route("/admin/user/<user_id>/send-reset", methods=["POST"])
@admin_required
def admin_send_reset(user_id):
    """Admin-triggered magic-login email — the user gets a sign-in link
    they can use to log in without their password, then rotate it from
    My Account."""
    target = db.get_user_by_id(user_id)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("admin_page"))
    try:
        import magic
        token = magic.make_magic_token(target["id"])
        site_url = os.environ.get(
            "SITE_URL",
            "https://poolboy2-app-production.up.railway.app"
        ).rstrip("/")
        link = f"{site_url}/m/{token}?next=/home"
        _send_reset_email(target, link)
        flash(f"Reset link emailed to {target['email']}.", "success")
    except Exception as e:
        flash(f"Failed to send reset: {e}", "error")
    return redirect(url_for("admin_page"))


@app.route("/admin/user/<user_id>/balance", methods=["POST"])
@admin_required
def admin_override_balance(user_id):
    """Override a user's balance on a specific pool. Records a 'adjustment'
    transaction with the delta so the timeline stays honest."""
    pool_id = (request.form.get("pool_id") or "").strip()
    if not pool_id:
        flash("Missing pool_id.", "error")
        return redirect(url_for("admin_page"))
    try:
        new_balance = float(request.form.get("balance"))
    except (TypeError, ValueError):
        flash("Balance must be a number.", "error")
        return redirect(url_for("admin_page"))
    current = db.get_member_balance(user_id, pool_id)
    delta = round(new_balance - float(current), 2)
    if abs(delta) < 0.005:
        flash("No change to balance.", "info")
        return redirect(url_for("admin_page"))
    db.write_adjustment_transaction(
        user_id, pool_id, delta,
        description=f"Admin adjustment to ${new_balance:.2f}"
    )
    flash(f"Balance updated by ${delta:+.2f}.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/scoring", methods=["POST"])
@admin_required
def save_scoring():
    user       = current_user()
    pool_id    = request.form.get("pool_id") or None
    round_type = request.form.get("round_type") or None

    try:
        win  = int(request.form.get("points_win", 3))
        draw = int(request.form.get("points_draw", 1))
        loss = int(request.form.get("points_loss", 0))
    except (KeyError, ValueError):
        flash("Please enter valid integer point values.", "error")
        return redirect(url_for("admin_page"))

    # Inherit economy values from the current effective config when fields
    # aren't present in the submitted form (e.g. per-pool forms only render
    # the points fields). Falling back to None would null out the column
    # on a per-pool override and silently revert the user's economy settings.
    stage_for_lookup = "Round of 16" if round_type == "knockout" else "Group A"
    current = db.get_scoring_config(pool_id, stage_for_lookup)

    def _float(key, default):
        try:
            return float(request.form[key])
        except (KeyError, ValueError, TypeError):
            return default

    starting_balance   = _float("starting_balance",                current["starting_balance"])
    group_win_payout   = _float("group_win_payout",                current["group_win_payout"])
    group_draw_payout  = _float("group_draw_payout",               current["group_draw_payout"])
    group_loss_payout  = _float("group_loss_payout",               current["group_loss_payout"])
    spy_base_cost      = _float("spy_base_cost",                   current["spy_base_cost"])
    spy_increment      = _float("spy_increment",                   current["spy_increment"])
    ko_flat_mult       = _float("knockout_flat_payout_multiplier", current["knockout_flat_payout_multiplier"])

    db.save_scoring_config(
        str(uuid.uuid4()), win, draw, loss, user["id"],
        pool_id=pool_id, round_type=round_type,
        starting_balance=starting_balance,
        group_win_payout=group_win_payout,
        group_draw_payout=group_draw_payout,
        group_loss_payout=group_loss_payout,
        spy_base_cost=spy_base_cost,
        spy_increment=spy_increment,
        knockout_flat_payout_multiplier=ko_flat_mult,
    )

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


@app.route("/admin/seed-euro2024", methods=["POST"])
@admin_required
def admin_seed_euro2024():
    import seed_euro2024 as s
    import io, sys
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        s.main()
    finally:
        sys.stdout = old_stdout
    output = buf.getvalue()
    last_line = [l for l in output.strip().splitlines() if l][-1] if output.strip() else "Done."
    flash(f"Euro 2024 sim seeded. {last_line}", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/fixture/<fixture_id>/result", methods=["POST"])
@admin_required
def set_fixture_result(fixture_id):
    """Record a match result and trigger economy payouts for all picks."""
    try:
        home_score = int(request.form["home_score"])
        away_score = int(request.form["away_score"])
    except (KeyError, ValueError):
        flash("Please enter valid scores.", "error")
        return redirect(url_for("admin_page"))

    result    = db.update_fixture_result(fixture_id, home_score, away_score)
    processed = db.process_fixture_result(fixture_id)
    flash(f"Result saved ({result}). Processed {processed} picks.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/fixture/<fixture_id>/odds", methods=["POST"])
@admin_required
def set_fixture_odds(fixture_id):
    """
    Set per-fixture H/A odds for a knockout match. Empty inputs clear the
    odds (revert to the global flat multiplier on payout).
    """
    def _odds(key):
        raw = (request.form.get(key) or "").strip()
        if not raw:
            return None
        try:
            v = float(raw)
        except ValueError:
            return False  # sentinel for invalid input
        if v <= 0:
            return False
        return v

    home = _odds("home_odds")
    away = _odds("away_odds")
    if home is False or away is False:
        flash("Odds must be positive numbers (or blank to clear).", "error")
        return redirect(url_for("admin_page"))

    db.set_fixture_odds(fixture_id, home, away)
    if home is None and away is None:
        flash("Odds cleared — fixture will use the global multiplier.", "success")
    else:
        flash(f"Odds saved — H: {home or 'flat'} · A: {away or 'flat'}.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/pool/create", methods=["POST"])
@admin_required
def create_pool():
    name                 = request.form.get("name", "").strip()
    description          = request.form.get("description", "").strip()
    is_public            = 1 if request.form.get("is_public") else 0
    entry_fee            = request.form.get("entry_fee", "").strip() or None
    payment_instructions = request.form.get("payment_instructions", "").strip() or None

    if not name:
        flash("Pool name is required.", "error")
        return redirect(url_for("admin_page"))

    db.create_pool(str(uuid.uuid4()), name, description, is_public,
                   entry_fee=entry_fee, payment_instructions=payment_instructions)
    flash(f"Pool '{name}' created.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/pool/<pool_id>/member/<user_id>/paid", methods=["POST"])
@admin_required
def mark_member_paid(pool_id, user_id):
    membership = db.get_pool_membership(user_id, pool_id)
    if not membership:
        flash("Member not found.", "error")
        return redirect(url_for("admin_page"))
    new_paid = 0 if membership["has_paid"] else 1
    db.set_member_paid(user_id, pool_id, new_paid)
    pool = db.get_pool_by_id(pool_id)
    target = db.get_user_by_id(user_id)
    verb = "marked as paid" if new_paid else "marked as unpaid"
    flash(f"{target['display_name']} {verb} for {pool['name']}.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/pool/<pool_id>/member/<user_id>/adjust-balance", methods=["POST"])
@admin_required
def admin_adjust_balance(pool_id, user_id):
    """Manual balance adjustment for a pool member (admin only)."""
    try:
        amount = float(request.form["amount"])
    except (KeyError, ValueError):
        flash("Invalid amount.", "error")
        return redirect(url_for("admin_page"))

    description = request.form.get("description", "Manual adjustment").strip() or "Manual adjustment"
    if not description:
        description = "Manual adjustment by admin"

    target = db.get_user_by_id(user_id)
    if not target or not db.is_pool_member(user_id, pool_id):
        flash("Member not found.", "error")
        return redirect(url_for("admin_page"))

    db.apply_balance_adjustment(str(uuid.uuid4()), user_id, pool_id, amount, description)
    sign = "+" if amount >= 0 else ""
    flash(f"Balance adjusted for {target['display_name']}: {sign}${amount:.2f}.", "success")
    return redirect(url_for("admin_page"))


# ── Pool stats page ────────────────────────────────────────────────────────

@app.route("/pool/<pool_id>/stats")
@login_required
def pool_stats(pool_id):
    """Same destination as /leaderboard — combined Stats page."""
    return _render_combined_stats(pool_id)


# ── Startup ────────────────────────────────────────────────────────────────

def seed_defaults():
    """
    Create a default admin user and a sample pool if the database is empty.
    Runs once on first startup.
    """
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
        db.create_user(
            user_id      = str(uuid.uuid4()),
            display_name = "Alice",
            email        = "alice@example.com",
            password_hash= generate_password_hash("password123"),
        )
        print("[seed] Created admin (admin@pool.local / changeme) and Alice (alice@example.com / password123)")

        pool_id = str(uuid.uuid4())
        db.create_pool(pool_id, "The Main Event", "Open picks pool for the 2026 World Cup", is_public=1)
        print(f"[seed] Created pool 'The Main Event' (id={pool_id})")


if __name__ == "__main__":
    db.init_db()
    seed_defaults()
    fx.sync_fixtures()
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, host="0.0.0.0", port=port)
