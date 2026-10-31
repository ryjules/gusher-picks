# Gushers Picks

Matchup boards, player props, model picks and a P&L tracker for college football, the NFL,
the NBA and college basketball. The site is a single page (`docs/index.html`) rebuilt twice a
day by a GitHub Action and served by GitHub Pages.

## How it updates

`.github/workflows/refresh.yml` runs at 8:40 AM and 4:40 PM Eastern (and on demand from the
Actions tab). It runs `build/fetch.py`, which pulls:

- ESPN: schedules, box scores, play-by-play, DraftKings lines and props, injuries
- Kalshi: fair prices for game lines and player props
- nflverse: NFL scores, consensus lines and player game logs (data check)
- weather.gov: kickoff forecasts for outdoor games
- CollegeFootballData / CollegeBasketballData: scores, multi-book lines, stadiums (needs `CFBD_API_KEY`)
- The Odds API: lines across US sportsbooks and NBA scores (needs `ODDS_API_KEY`)

It then writes `docs/index.html` and appends the day's green TAKE picks to
`docs/data/modelpicks.json`, which drives the hypothetical model P&L.

## One-time setup

1. Push this folder to a new GitHub repository.
2. Settings → Pages → Build and deployment: "Deploy from a branch", branch `main`, folder `/docs`.
3. Settings → Secrets and variables → Actions → New repository secret:
   `CFBD_API_KEY` and `ODDS_API_KEY`.
4. Actions tab → "Refresh picks" → Run workflow, to build it once right away.
5. Optional custom domain: Settings → Pages → Custom domain, then add the DNS records GitHub shows.

## My Plays

On the standalone site, your plays and unit size are saved in your browser (per device).
