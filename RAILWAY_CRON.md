# Live data architecture

The pool page fetches live match data straight from ESPN's public
scoreboard at request time (cached in-memory for 30 seconds — see
`live_now_helper.py`). No cron is required to keep the banner fresh —
each page load and each `/pool/<id>/live-now` poll hits the same cache.

## What the GitHub Actions cron still does

`.github/workflows/auto-score.yml` still runs every 3 minutes during
the match window. Its only job now is to:

  1. Detect FINISHED matches via the football-data.org API
  2. Settle them via `process_fixture_result()` (writes payouts)

GitHub Actions throttling (the cron firing roughly once an hour instead
of every 3 min) is no longer felt by users — even a 30+ minute delay
on payout settlement is acceptable, since live scores already update
through ESPN.

## If you want faster settlement

Adding a Railway cron service (recommended only if you want payouts
landing within a minute of full time):

  1. Railway dashboard → your project → New Service → Empty Service
  2. Connect to the same GitHub repo
  3. Settings → Deploy → Custom Start Command:
     `python3 sim/auto_score_from_api.py --apply`
  4. Settings → Cron Schedule: `*/3 * * * *`
  5. Variables → reference `DATABASE_URL` + `FOOTBALL_DATA_API_KEY`
     from the web service

Disable the GitHub workflow once Railway is verified (Actions tab →
... → Disable workflow) to avoid duplicate writes.
