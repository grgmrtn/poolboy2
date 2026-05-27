# WC26 Pool — World Cup 2026 Picks App

A dark, monospace-styled picks pool app for World Cup 2026.

## Quick start

```bash
# 1. Install the one dependency
pip install flask

# 2. Run
python3 app.py
```

Then open http://localhost:5000

**Default accounts (created on first run):**
| Email | Password | Role |
|---|---|---|
| admin@pool.local | changeme | Admin |
| alice@example.com | password123 | User |

## Project structure

```
worldcup-pool/
├── app.py          # Flask routes and auth
├── database.py     # All DB setup and query helpers
├── fixtures.py     # Fixture syncing (API or mock data)
├── instance/
│   └── pool.db     # SQLite database (auto-created)
└── templates/
    ├── base.html   # Navbar, design system, layout
    ├── login.html  # Login page
    ├── home.html   # Pool browser + my pools
    ├── pool.html   # Fixture picker + leaderboard
    └── admin.html  # Scoring config + result entry
```

## Live fixtures (optional)

Get a free API key from https://www.football-data.org then:

```bash
export FOOTBALL_DATA_API_KEY=your_key_here
python3 app.py
```

Without a key, 24 realistic mock fixtures are loaded automatically.

## How scoring works

1. Admin enters match results on /admin
2. Backend compares each pick's prediction to the actual result
3. Points (configurable: default Win=3, Draw=1, Loss=0) are written to `score_log`
4. Scores appear live in the navbar and pool leaderboard

## Production notes

- Change `SECRET_KEY` in app.py to a long random string
- Remove the dev account hints from login.html
- Consider PostgreSQL for multi-user deployments (swap sqlite3 for psycopg2)
