# NHL board: build plan

Goal: a fifth league tab, **NHL**, built like JD's hockey sheet (Profit Hunt, Nov 4 2026), with confidence
levels for **Moneyline, Puck line and Total**. Same pipeline as basketball: `build/fetch.py` writes `nhl` and
`nhl_meta` into games.json, `build/board.template.html` renders it, refresh.yml runs it.

## Data (checked Nov 4: everything is on ESPN's free API)

- Scoreboard: `https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates=YYYYMMDD`
  - `competitors[].probables[]`: probable goalie per team (name, id). Confirmation can be added later.
  - `competitions[0].odds[0]`: DraftKings moneyline, puck line (+/-1.5 with prices) and total.
- Summary: `.../hockey/nhl/summary?event=ID`
  - `boxscore.teams[].statistics`: shotsTotal, hits, blockedShots, powerPlayGoals/Opportunities/Pct,
    penaltyMinutes, faceoffPercent, giveaways, takeaways.
  - `boxscore.players[]` goalies: GA, SA, SV, SV%, TOI; skaters: G, A, S, SOG, BS, HT, TOI, PPTOI.
- Kalshi: NHL game markets (winner, totals, puck line) through the same `kalshi_*` helpers as NBA.

## Sheet layout (match JD's)

Header: records, streak, rest days, last 5 scores, puck line, O/U, moneylines, off/def/overall rank.

1. **Goalies**: probable starter each side (P = probable, green = confirmed), games started, record, GAA.
   Rows: Goals Allowed, Saves, Save%, Shots Against, Win% (season, away/home, last 10, last 5, L5 rank).
2. **Offense**: Goals/G, Shots/G, SOG/G, Hits/G, PP%, Blocks/G.
3. **Defense**: Opp Goals/G, Opp Shots/G, Opp SOG/G, Opp Hits/G, PK%, Opp Blocks/G.
4. ADV column, as on the other boards.
5. Confidence: Moneyline, Puck line, Total.

## Confidence model (start simple, tune with results)

- Unit matchups, as in basketball: offense goals/SOG/PP% against the other side's goals allowed/SOG allowed/PK%,
  plus the **goalie matchup** (save % and goals allowed, last 5 weighted, with the season blended in).
- Total: projected goals = each offense's recent goals against the other goalie and defense, then compare with the
  line in goals (one goal of edge is roughly a full score point; tune it).
  JD's rule from the video: **"worst offense vs a top-10 goalie = under"**, and he passes when the projection sits
  within a few tenths of the line (CHI/FLA 5.4 against 5.5).
- Puck line: moneyline strength and the 1.5-goal margin; favorites at -1.5 need a big edge.
- The goalie is the biggest single input, and probables change around 1-2 PM ET: refresh then (add an NHL-only
  run to refresh.yml), and flag "goalie not confirmed" as a warning, so the pick is a small bet at most.
- Same rules as the other sports: no -300 or shorter moneylines, line moves and trends as information only,
  the 4-7 Pass display band, colors.

## Props (phase 2)

Shots on goal, points, assists, goals, saves (JD's tabs), with the same game-log drop-down (last 5: G, A, S, SOG,
TOI, PPTOI). JD's filters: 4 of 5 hit rate (he passes on 3/5), plus-money or close, and a good matchup
(opponent SOG allowed rank).

## Grading

Add `nhl` to `record_finals` / modelpicks grading, and to Top Picks, Most confident, My Plays and Bankroll.
