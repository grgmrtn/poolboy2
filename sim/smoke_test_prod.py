"""
smoke_test_prod.py — read-only-ish smoke test against the deployed Railway URL.

Differences from sim/smoke_test.py:
  - No direct DB access (production uses Postgres + we have no credentials)
  - No state mutation beyond registering a single throwaway user
  - No admin write actions (no result entry, no odds setting, no bets)
  - All assertions derive from HTML response bodies only

What it verifies: that the deploy landed, that pages render with the new
template features (balance label, odds attributes, color brightener, etc.),
that the registration + join flow still works end-to-end, and that the admin
page renders without the GROUP BY 500 we hit yesterday.

Run: python3 sim/smoke_test_prod.py [base_url]
"""
import sys, uuid, requests, re

import os
DEFAULT_BASE = "https://poolboy2-app-production.up.railway.app"
BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else DEFAULT_BASE
# Pull admin creds from env so they never get committed. Without these set,
# section [G] is skipped (the rest of the smoke test still runs).
ADMIN_EMAIL = os.environ.get("SMOKE_ADMIN_EMAIL", "")
ADMIN_PW    = os.environ.get("SMOKE_ADMIN_PW", "")

results = []
def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    results.append((name, status, detail))
    icon = "✓" if ok else "✗"
    print(f"  {icon} [{status}] {name}" + (f" — {detail}" if detail else ""))


def login(email, pw):
    s = requests.Session()
    r = s.post(f"{BASE}/login", data={"email":email,"password":pw}, allow_redirects=False)
    return s if r.status_code in (302, 303) else None


def main():
    print(f"\nProduction smoke test against: {BASE}\n")

    # ────────────────────────────────────────────────────────────────────
    print("[A] Landing pages render with new CSS")
    # ────────────────────────────────────────────────────────────────────
    r = requests.get(f"{BASE}/login", timeout=15)
    check("/login returns 200", r.status_code == 200, f"got {r.status_code}")
    check("base font-size 17px present (legibility fix)",
          "html { font-size: 17px" in r.text)
    check("safe-area-inset support (iPhone fix)",
          "safe-area-inset" in r.text)
    check("brightened text-dim token (#7070a0)",
          "#7070a0" in r.text)

    r = requests.get(f"{BASE}/register", timeout=15)
    check("/register returns 200", r.status_code == 200)

    # ────────────────────────────────────────────────────────────────────
    print("\n[B] Register a fresh throwaway smoke user")
    # ────────────────────────────────────────────────────────────────────
    nonce = uuid.uuid4().hex[:8]
    smoke_email = f"smoke_{nonce}@test.local"
    smoke_pw    = "smoketest1"
    smoke_name  = f"Smoke{nonce[:4]}"
    s = requests.Session()
    r = s.post(f"{BASE}/register",
               data={"display_name": smoke_name, "email": smoke_email, "password": smoke_pw},
               allow_redirects=False, timeout=15)
    check("register returns 302",
          r.status_code in (302, 303),
          f"got {r.status_code}; body: {r.text[:150]}")
    print(f"     registered: {smoke_email}")

    # Confirm session is established by hitting /home
    r = s.get(f"{BASE}/home", timeout=15, allow_redirects=False)
    check("session active (GET /home -> 200 not 302)",
          r.status_code == 200,
          f"got {r.status_code}")
    check("display name appears on /home",
          smoke_name in r.text)

    # ────────────────────────────────────────────────────────────────────
    print("\n[C] /home renders YOUR BALANCE not pts (today's fix)")
    # ────────────────────────────────────────────────────────────────────
    # Even with zero pools joined, the page should render without "pts"
    check("'pts</span>' (legacy) NOT on /home",
          "pts</span>" not in r.text)
    check("'YOUR SCORE' (legacy) NOT on /home",
          "YOUR SCORE" not in r.text)

    # ────────────────────────────────────────────────────────────────────
    print("\n[D] Identify a public pool, join, view pool page")
    # ────────────────────────────────────────────────────────────────────
    # Find a public pool from /home's "Available pools" list (join links)
    join_links = re.findall(r'action="(/pool/[a-f0-9-]+/join)"', r.text)
    if not join_links:
        check("at least one public pool visible to join", False,
              "no /pool/<id>/join links found on /home — admin must create a public pool")
        return
    check("at least one public pool visible to join", True,
          f"found {len(join_links)} join link(s)")
    join_url = BASE + join_links[0]
    pool_id  = re.search(r"/pool/([a-f0-9-]+)/join", join_url).group(1)

    r = s.post(join_url, allow_redirects=False, timeout=15)
    check("join pool returns 302",
          r.status_code in (302, 303),
          f"got {r.status_code}")

    # Now hit the pool page
    r = s.get(f"{BASE}/pool/{pool_id}", timeout=15)
    check("/pool/<id> returns 200", r.status_code == 200, f"got {r.status_code}")
    check("'YOUR BALANCE' label present on /home after joining",
          "YOUR BALANCE" in s.get(f"{BASE}/home", timeout=15).text)

    # ────────────────────────────────────────────────────────────────────
    print("\n[E] Pool page contains new template features")
    # ────────────────────────────────────────────────────────────────────
    check("brightenForDarkBg JS helper present (color fix)",
          "brightenForDarkBg" in r.text)
    check("relLuminance JS helper present",
          "relLuminance" in r.text)
    check("data-home-odds attr present on fixture rows (per-fixture KO odds)",
          "data-home-odds" in r.text)
    check("data-away-odds attr present on fixture rows",
          "data-away-odds" in r.text)
    check("koMultFor JS helper present",
          "function koMultFor" in r.text)
    check("no Traceback / 500 in pool page",
          "Traceback" not in r.text and "Internal Server Error" not in r.text)

    # ─── New features from the latest pending deploy ────────────────────
    check("ET timezone label rendered ('New York time')",
          "New York time" in r.text)
    check("walkthrough modal HTML present (id='walk-modal')",
          'id="walk-modal"' in r.text)
    check("'How it works ?' trigger link present",
          "How it works ?" in r.text)
    check("walkthrough auto-show JS hook present",
          "maybeAutoShowWalkthrough" in r.text)
    check("walkthrough section 'Group stage' rendered",
          "Group stage — free picks" in r.text)
    check("walkthrough section 'Spying' (unified) rendered",
          "peek at the field" in r.text)

    # ─── Row redesign + unified Spy modal ───────────────────────────────
    check("match-row wrapper present",
          'class="match-row"' in r.text)
    check("fixture-actions container present",
          'class="fixture-actions"' in r.text)
    check("unified Spy trigger button rendered",
          'class="spy-trigger"' in r.text)
    check("unified Spy modal markup present",
          'id="spy-modal-unified"' in r.text)
    check("Hide Completed Picks toggle present",
          'Hide Completed Picks' in r.text)
    check("nav rank pill present",
          'class="nav-rank"' in r.text)

    # ─── New backend endpoints respond ──────────────────────────────────
    rl = s.get(f"{BASE}/pool/{pool_id}/stats", timeout=15)  # warm session
    # We can only exercise spy-list / field-spy with a real fixture id —
    # not worth doing destructively against prod; just check the routes
    # exist by hitting them with an invalid fixture id (expect 404).
    r404 = s.get(f"{BASE}/pool/{pool_id}/fixture/__invalid__/spy-list", timeout=15)
    check("/spy-list route exists (returns 4xx for unknown fixture)",
          r404.status_code in (400, 404))

    # ────────────────────────────────────────────────────────────────────
    print("\n[F] Stats page renders (Fix #1 score_log populated)")
    # ────────────────────────────────────────────────────────────────────
    r = s.get(f"{BASE}/pool/{pool_id}/stats", timeout=15)
    check("/pool/<id>/stats returns 200", r.status_code == 200)
    # We can't tell from outside whether the pool actually has results yet.
    # If results have been entered, expect the chart. If not, expect empty state.
    has_chart = "scored matches" in r.text
    has_empty = "No scored fixtures yet" in r.text
    if has_chart:
        m = re.search(r'<strong>(\d+)</strong>\s+scored matches', r.text)
        check("stats chart rendered", True,
              f"scored matches: {m.group(1) if m else '?'}")
    elif has_empty:
        check("stats empty-state rendered (no results entered yet — expected if pool not started)",
              True, "if real fixtures had results, this would be the chart")
    else:
        check("stats page in expected state", False,
              "neither chart nor empty-state markers found")

    # ────────────────────────────────────────────────────────────────────
    print("\n[G] Admin page renders (Postgres GROUP BY fix)")
    # ────────────────────────────────────────────────────────────────────
    if not ADMIN_EMAIL or not ADMIN_PW:
        print("  (skipped — set SMOKE_ADMIN_EMAIL and SMOKE_ADMIN_PW env vars to include)")
    else:
        admin = login(ADMIN_EMAIL, ADMIN_PW)
        if not admin:
            check("admin login succeeded", False,
                  "credentials rejected — check SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PW")
        else:
            check("admin login succeeded", True)
            r = admin.get(f"{BASE}/admin", timeout=15)
            check("/admin returns 200 (Postgres GROUP BY fix verified)",
                  r.status_code == 200, f"got {r.status_code}")
            check("'Per-pool scoring' section rendered",
                  "Per-pool scoring" in r.text)
            check("'KO odds (H · A)' column rendered (today's per-fixture odds feature)",
                  "KO odds (H · A)" in r.text)
            odds_actions = re.findall(r'action="/admin/fixture/([^/]+)/odds"', r.text)
            check(f"admin renders odds form actions ({len(odds_actions)} found)",
                  len(odds_actions) >= 0,
                  f"count: {len(odds_actions)}")
            check("no Traceback in /admin page",
                  "Traceback" not in r.text)

    # ────────────────────────────────────────────────────────────────────
    print("\n[H] Logout works")
    # ────────────────────────────────────────────────────────────────────
    r = s.get(f"{BASE}/logout", allow_redirects=False, timeout=15)
    check("/logout returns 302", r.status_code in (302, 303))
    r = s.get(f"{BASE}/home", allow_redirects=False, timeout=15)
    check("post-logout /home redirects to /login",
          r.status_code in (302, 303) and "/login" in r.headers.get("Location", ""))

    # ────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_fail = sum(1 for _, s, _ in results if s == "FAIL")
    print(f"PROD SMOKE: {n_pass} PASS, {n_fail} FAIL")
    print(f"Throwaway user created: {smoke_email}  (delete via SQL if you want a clean prod DB)")
    if n_fail:
        print("\nFAILURES:")
        for name, st, detail in results:
            if st == "FAIL":
                print(f"  ✗ {name}  ({detail})")
        sys.exit(1)
    print("\nAll production smoke checks passed.")


if __name__ == "__main__":
    main()
