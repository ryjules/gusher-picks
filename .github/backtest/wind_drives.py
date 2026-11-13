"""One-off backtest (Nov 13): wind and stalled-drive rules for football totals.

For every college (FBS) and NFL game of 2024-2026: closing total (ESPN pickcenter), final score, kickoff wind and
gusts (Open-Meteo archive at the venue), and each team's 3rd-down rate and turnover margin over its previous 3 games
and season to date. Writes one CSV row per game.
"""
import csv, json, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

OUT = sys.argv[1]
B = {"cfb": "football/college-football", "nfl": "football/nfl"}


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(1 + 2 ** i)
    return None


def events(lg, year):
    out = {}
    weeks = range(1, 17) if lg == "cfb" else range(1, 19)
    for wk in weeks:
        q = f"seasontype=2&week={wk}&dates={year}&limit=400" + ("&groups=80" if lg == "cfb" else "")
        s = get(f"https://site.api.espn.com/apis/site/v2/sports/{B[lg]}/scoreboard?{q}") or {}
        for e in s.get("events", []):
            c = e["competitions"][0]
            if c["status"]["type"]["state"] != "post":
                continue
            out[e["id"]] = {"lg": lg, "id": e["id"], "date": e["date"], "season": year, "week": wk,
                            "neutral": c.get("neutralSite", False), "indoor": (c.get("venue") or {}).get("indoor", False),
                            "city": ((c.get("venue") or {}).get("address") or {}).get("city"),
                            "state": ((c.get("venue") or {}).get("address") or {}).get("state")}
    return out


def summary(g):
    s = get(f"https://site.api.espn.com/apis/site/v2/sports/{B[g['lg']]}/summary?event={g['id']}") or {}
    pc = [p for p in s.get("pickcenter", []) if p.get("overUnder")]
    g["total"] = pc[0]["overUnder"] if pc else None
    teams = {}
    for c in ((s.get("header") or {}).get("competitions") or [{}])[0].get("competitors", []):
        teams[c["homeAway"]] = {"tid": c["team"]["id"], "abbr": c["team"].get("abbreviation"), "pts": float(c.get("score") or 0)}
    for t in (s.get("boxscore") or {}).get("teams", []):
        side = "home" if t.get("homeAway") == "home" else "away"
        st = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        try:
            a, b = (st.get("thirdDownEff") or "0-0").split("-")
            teams.setdefault(side, {}).update({"3c": int(a), "3a": int(b), "to": int(st.get("turnovers") or 0)})
        except ValueError:
            pass
    g["teams"] = teams
    return g


GEO = {}


def geo(city, state):
    k = (city, state)
    if k not in GEO:
        r = get("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": city, "count": 10, "country": "US"})) or {}
        res = r.get("results", [])
        hit = next((x for x in res if state and x.get("admin1") == state), res[0] if res else None)
        GEO[k] = (hit["latitude"], hit["longitude"]) if hit else None
    return GEO[k]


ST = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
      "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
      "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
      "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
      "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
      "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
      "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
      "WY": "Wyoming", "DC": "District of Columbia"}


def weather(g):
    if g.get("indoor") or not g.get("city"):
        return g
    ll = geo(g["city"], ST.get(g.get("state") or "", g.get("state")))
    if not ll:
        return g
    t = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
    day = t.strftime("%Y-%m-%d")
    r = get("https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode({
        "latitude": ll[0], "longitude": ll[1], "start_date": day, "end_date": (t + timedelta(days=1)).strftime("%Y-%m-%d"),
        "hourly": "wind_speed_10m,wind_gusts_10m,precipitation", "wind_speed_unit": "mph", "timezone": "GMT"})) or {}
    h = r.get("hourly") or {}
    times = h.get("time") or []
    k0 = t.strftime("%Y-%m-%dT%H:00")
    if k0 in times:
        i = times.index(k0)
        win = range(i, min(i + 3, len(times)))
        g["wind"] = max((h["wind_speed_10m"][j] or 0) for j in win)
        g["gust"] = max((h["wind_gusts_10m"][j] or 0) for j in win)
        g["precip"] = sum((h["precipitation"][j] or 0) for j in win)
    return g


games = {}
for lg in ("cfb", "nfl"):
    for yr in (2024, 2025, 2026):
        games.update(events(lg, yr))
        print(lg, yr, len(games), file=sys.stderr)
with ThreadPoolExecutor(8) as ex:
    list(ex.map(summary, games.values()))
with ThreadPoolExecutor(4) as ex:
    list(ex.map(weather, games.values()))
with open(OUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["lg", "season", "week", "id", "date", "neutral", "indoor", "total", "wind", "gust", "precip",
                "away", "away_pts", "away_3c", "away_3a", "away_to", "home", "home_pts", "home_3c", "home_3a", "home_to"])
    for g in sorted(games.values(), key=lambda x: x["date"]):
        a, h = g.get("teams", {}).get("away", {}), g.get("teams", {}).get("home", {})
        w.writerow([g["lg"], g["season"], g["week"], g["id"], g["date"], g["neutral"], g["indoor"], g.get("total"), g.get("wind"),
                    g.get("gust"), g.get("precip"), a.get("tid"), a.get("pts"), a.get("3c"), a.get("3a"), a.get("to"),
                    h.get("tid"), h.get("pts"), h.get("3c"), h.get("3a"), h.get("to")])
print("rows", len(games), "with wind", sum(1 for g in games.values() if g.get("wind") is not None), file=sys.stderr)
