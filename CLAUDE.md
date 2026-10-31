# Gushers Picks

The site exists in two places that must stay identical:

1. **GitHub Pages** (this repo): https://ryjules.github.io/gusher-picks/
   - `build/fetch.py`, `build/inject.py`, `build/board.template.html`: the pipeline and page template
   - `docs/index.html`: the built page; `docs/data/modelpicks.json`: model pick history
   - `.github/workflows/refresh.yml` rebuilds at 8:40 AM and 4:40 PM ET
2. **claude.ai artifact**: https://claude.ai/artifact/LYteKqEiXFDBZQATguf3C3
   (slug `9e518492-d32d-4915-85ba-65c092cdd158`). Two scheduled tasks refresh it, and they
   download the pipeline from this repo's `main` branch, so the repo is the single source of code.

## Rule for every change

Make the change in `build/`, rebuild, then publish to **both** places in the same turn:

1. `python3 -I build/fetch.py cache games.json docs/data/modelpicks.json docs/data/modelpicks.json`
   then `python3 -I build/inject.py build/board.template.html games.json docs/index.html`
2. Commit and push to `main` (GitHub Pages redeploys).
3. Republish the artifact: `index.html` from `docs/index.html`, plus `src/fetch.py.txt`,
   `src/inject.py.txt`, `src/board.template.html` and `data/modelpicks.json`.

Don't tell the user a change is live until both publishes succeed.

## Notes

- My Plays: on claude.ai the artifact's private database; on GitHub Pages the browser's localStorage.
- API keys: `CFBD_API_KEY` and `ODDS_API_KEY` (GitHub Actions secrets, and the Claude environment).
- Page scripts can't use apostrophes inside single-quoted JS strings; run `node --check` on the
  extracted script before publishing.
