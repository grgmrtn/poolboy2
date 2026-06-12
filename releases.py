"""
releases.py — what's-new feed shown in the pool page modal.

Append the newest release to the TOP of this list. Each entry needs:
    date     — ISO YYYY-MM-DD; also drives the "unseen" dot via
               localStorage (we compare the latest date to the last
               release the user opened).
    title    — short headline.
    changes  — list of one-liners describing what shipped.
    emoji    — optional decorative glyph next to the title.

To ship a release: edit this file, commit, push, `railway up`. No DB
migration, no schema change.
"""

RELEASES = [
    {
        "date":  "2026-06-12",
        "emoji": "🎯",
        "title": "Group standings + per-group pick pages",
        "changes": [
            "Standings now show all four teams per group from day zero — no more empty tables",
            "Click any group header in the TBL view to open a focused pick page for that group",
            "Match pills on the group page are visually identical to the main pool",
            "Standings table reflows on mobile so the Pts column stays visible",
            "TBL stage tab now scrolls + auto-expands correctly",
            "Odds toggle hidden until the knockouts start",
        ],
    },
    {
        "date":  "2026-06-12",
        "emoji": "⚡",
        "title": "Live in-play scores in the match pill",
        "changes": [
            "Match pill shows a double-height lime score + pulsing LIVE tag while a game is on",
            "Auto-score cron tightened to every 3 minutes during games (was every 10)",
            "Manual workflow dispatches now apply by default — Dry-run is an opt-in checkbox",
        ],
    },
    {
        "date":  "2026-06-12",
        "emoji": "📊",
        "title": "Stats page rebuilt — balance over time",
        "changes": [
            "Each player's line plots running balance against a real time axis",
            "Per-event nodes — every payout, spy, bet, and adjustment is a point",
            "Hover any line to see just that player's events; their name floats at the line end",
            "Tiny jitter keeps overlapping lines individually hoverable",
            "Chart text now uses JetBrains Mono to match the rest of the site",
        ],
    },
    {
        "date":  "2026-06-12",
        "emoji": "🔎",
        "title": "Picker tooltips on the Completed table",
        "changes": [
            "Hover home, score, or away in the Completed table → see who picked that side",
            "Country-coloured border + header; solid dark background so names stay legible",
            "Mobile: tap a side, tooltip anchors to the row centre + a triangle shows which side is active",
        ],
    },
    {
        "date":  "2026-06-11",
        "emoji": "🤖",
        "title": "Auto-scoring + payout reconciliation",
        "changes": [
            "GitHub Actions cron pulls FINISHED scores from football-data.org",
            "Now correctly fires the economy-aware payout job (was writing to score_log only)",
            "Pick-reminder emails use the site's mono + lime aesthetic and one-click magic-login links",
            "Reminder emails go to every member (paid or not); admin always gets a CC copy",
        ],
    },
    {
        "date":  "2026-06-10",
        "emoji": "🏆",
        "title": "Team names + my-account dialog",
        "changes": [
            "Optional team alias separate from your real first name (leaderboard shows the team; spy reveals stay real)",
            "Edit them any time via the My Account dialog at the top of the pool",
            "Now includes a password-change form too",
            "Admin can fix names from the admin panel",
            "Pool-intro banner can be permanently dismissed; replaced by a How it Works pill",
        ],
    },
    {
        "date":  "2026-06-09",
        "emoji": "💰",
        "title": "Dynamic KO spy pricing + 90-day sessions",
        "changes": [
            "Knockout field-spy now scales with the pot — max(base, min(10%·pot, $20))",
            "Login persists for 90 days so the daily \"sign in again\" tax is gone",
            "Compact recap table for the Completed stage; DONE tab pinned first in the nav",
        ],
    },
]


def latest_release_date():
    """Highest date among all releases — drives the unseen-dot logic."""
    return max((r["date"] for r in RELEASES), default="")
