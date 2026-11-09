"""Pull ESPN data for this week's FBS + NFL slates and build the matchup board's data file.

Usage: python3 -I fetch.py <cache_dir> <out_json>
"""
import json, math, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo
import numpy as np

CACHE, OUT = sys.argv[1], sys.argv[2]
os.makedirs(CACHE, exist_ok=True)
BASE = "https://site.api.espn.com/apis/site/v2/sports/football"
ET = ZoneInfo("America/New_York")
PASS_TYPES = {"Pass Reception", "Passing Touchdown", "Pass Incompletion", "Sack", "Interception", "Pass Interception Return",
              "Interception Return Touchdown", "Pass"}
RUSH_TYPES = {"Rush", "Rushing Touchdown"}
# NFL play-by-play uses older team codes in penalty descriptions.
PBP_ALIASES = {"ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU", "SL": "LAR", "LA": "LAR"}
TO_TYPES = {"Interception", "Pass Interception Return", "Interception Return Touchdown", "Fumble Recovery (Opponent)",
            "Fumble Return Touchdown", "Sack Fumble Recovery (Opponent)"}


def get(url, cache_name=None, retries=4):
    path = os.path.join(CACHE, cache_name) if cache_name else None
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
            if path:
                with open(path, "w") as f:
                    json.dump(data, f)
            return data
        except Exception as e:  # network hiccup: back off and retry (longer when rate-limited)
            if i == retries - 1:
                print("FAIL", url, e, file=sys.stderr)
                return None
            time.sleep((5 if "429" in str(e) else 1) * 2 ** i)


def num(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse_summary(d, lg):
    """Reduce one completed game's summary to a record per team."""
    comp = d["header"]["competitions"][0]
    teams = {}
    for c in comp["competitors"]:
        ls = [num(x.get("displayValue")) for x in (c.get("linescores") or [])]
        teams[c["team"]["id"]] = {"id": c["team"]["id"], "abbr": c["team"].get("abbreviation", "?"), "ha": c["homeAway"],
                                  "pts": num(c.get("score")), "h1": sum(ls[:2]) if ls else None}
    ids = list(teams)
    if len(ids) != 2:
        return None
    for t in d.get("boxscore", {}).get("teams", []):
        rec = teams.get(t["team"]["id"])
        if rec is None:
            continue
        st = {s["name"]: s.get("displayValue") for s in t.get("statistics", [])}
        rec["passY"] = num(st.get("netPassingYards"))
        rec["rushY"] = num(st.get("rushingYards"))
        ca = (st.get("completionAttempts") or "0/0").replace("-", "/").split("/")
        rec["passAtt"] = num(ca[1] if len(ca) > 1 else 0)
        rec["rushAtt"] = num(st.get("rushingAttempts"))
        td = (st.get("thirdDownEff") or "0-0").split("-")
        rec["d3c"], rec["d3a"] = num(td[0]), num(td[1] if len(td) > 1 else 0)
        rec["to"] = num(st.get("turnovers"))
        pen = (st.get("totalPenaltiesYards") or "0-0").split("-")
        rec["penY"] = num(pen[1] if len(pen) > 1 else 0)
        if st.get("sacksYardsLost"):
            rec["sacksTaken"] = num(st["sacksYardsLost"].split("-")[0])
    for t in d.get("boxscore", {}).get("players", []):
        rec = teams.get(t["team"]["id"])
        if rec is None:
            continue
        for cat in t.get("statistics", []):
            labels = cat.get("labels") or []
            tot = cat.get("totals") or []
            if not tot:
                tot = [sum(num(a["stats"][i]) for a in cat.get("athletes", []) if i < len(a.get("stats", [])))
                       for i in range(len(labels))]
            vals = dict(zip(labels, tot))
            if cat["name"] == "passing":
                rec["passTD"] = num(vals.get("TD"))
            elif cat["name"] == "rushing":
                rec["rushTD"] = num(vals.get("TD"))
            elif cat["name"] == "defensive":
                rec["sacks"] = num(vals.get("SACKS"))
    if any("passY" not in teams[i] for i in ids):
        return None
    if lg == "nfl":
        for t in d.get("boxscore", {}).get("players", []):
            rec = teams.get(t["team"]["id"])
            if rec is None:
                continue
            pl = rec.setdefault("players", {})
            for cat in t.get("statistics", []):
                labels = cat.get("labels") or []
                for a in cat.get("athletes", []):
                    ath = a.get("athlete") or {}
                    if not ath.get("id"):
                        continue
                    v = dict(zip(labels, a.get("stats", [])))
                    p = pl.setdefault(ath["id"], {"name": ath.get("shortName") or ath.get("displayName"),
                                                  "full": ath.get("displayName")})
                    if cat["name"] == "passing":
                        p["passYds"] = num(v.get("YDS")); p["passTD"] = num(v.get("TD"))
                        p["passAtt"] = num((v.get("C/ATT") or "0/0").split("/")[-1])
                        p["cmp"] = num((v.get("C/ATT") or "0/0").split("/")[0]); p["int"] = num(v.get("INT"))
                    elif cat["name"] == "rushing":
                        p["rushYds"] = num(v.get("YDS")); p["rushTD"] = num(v.get("TD")); p["car"] = num(v.get("CAR"))
                        p["rushLong"] = num(v.get("LONG"))
                    elif cat["name"] == "receiving":
                        p["recYds"] = num(v.get("YDS")); p["recTD"] = num(v.get("TD")); p["rec"] = num(v.get("REC"))
                        p["tgt"] = num(v.get("TGTS")); p["recLong"] = num(v.get("LONG"))
                    elif cat["name"] == "defensive":
                        p["tackles"] = num(v.get("TOT")); p["sacks"] = num(v.get("SACKS"))
                    elif cat["name"] == "kicking":
                        p["fg"] = num((v.get("FG") or "0/0").split("/")[0])
    # Play-by-play: first-half splits, plus full-game sacks (box-score sack totals are unreliable).
    names = {}
    for c in comp["competitors"]:
        t = c["team"]
        names[t["id"]] = {re.sub(r"[^A-Z&]", "", x.upper()) for x in [t.get("abbreviation"), t.get("location"), t.get("name"),
                          t.get("nickname"), t.get("shortDisplayName"), t.get("displayName")] if x}
        # Initials as play-by-play writers use them: Wake Forest -> WF/WFU, Missouri -> MU, Maryland -> UMD.
        ini = "".join(w[0] for w in re.findall(r"[A-Za-z]+", t.get("location") or "")).upper()
        ab = (t.get("abbreviation") or "").upper()
        names[t["id"]] |= {x for x in [ini, ini + "U", "U" + ini, "U" + ab] if len(x) >= 2}

    def who(token):
        """Match the team token in a penalty description ("UNC", "Akron", "LU") to a team id."""
        tok = PBP_ALIASES.get(token.upper(), token.upper())
        hits = [i for i in ids if tok in names.get(i, ())]
        if not hits:
            hits = [i for i in ids if len(tok) >= 3 and any(n.startswith(tok) or (len(tok) >= 4 and tok in n) for n in names.get(i, ()))]
        return hits[0] if len(hits) == 1 else None
    h1 = {i: dict(passY=0.0, rushY=0.0, passTD=0, rushTD=0, sacks=0, plays=0, d3c=0, d3a=0, to=0, penY=0.0) for i in ids}
    full_plays = {i: 0 for i in ids}
    full_sacks = {i: 0 for i in ids}
    have_pbp = False
    for drive in d.get("drives", {}).get("previous", []):
        off = (drive.get("team") or {}).get("id")
        plays = drive.get("plays", [])
        for n, p in enumerate(plays):
            have_pbp = True
            ty = (p.get("type") or {}).get("text", "")
            per = (p.get("period") or {}).get("number", 0)
            text = p.get("text") or ""
            if per <= 2 and "PENALTY" in text.upper() and not re.search(r"declined|offsetting", text, re.I):
                for ab, yds in re.findall(r"PENALTY (?:on )?([A-Za-z&]+)[\s-].*?(\d+) yards?", text, re.I):
                    tid = who(ab)
                    if tid:
                        h1[tid]["penY"] += float(yds)
            if off not in h1:
                continue
            is_pass, is_rush = ty in PASS_TYPES, ty in RUSH_TYPES
            if is_pass or is_rush:
                full_plays[off] += 1
            if ty.startswith("Sack"):
                full_sacks[off] += 1
            if per > 2:
                continue
            o = h1[off]
            y = num(p.get("statYardage"))
            if is_pass or is_rush:
                o["plays"] += 1
                # College stats count sack yardage as rushing; the NFL counts it against passing.
                if is_rush or (ty.startswith("Sack") and lg == "cfb"):
                    o["rushY"] += y
                else:
                    o["passY"] += y
                start = p.get("start") or {}
                if start.get("down") == 3:
                    o["d3a"] += 1
                    nxt = next((q for q in plays[n + 1:] if (q.get("start") or {}).get("down")), None)
                    if "Touchdown" in ty or (nxt and nxt["start"].get("down") == 1
                                             and (nxt["start"].get("team") or {}).get("id") == (start.get("team") or {}).get("id")):
                        o["d3c"] += 1
            # ESPN sometimes files an offensive touchdown under the plain play type ("Pass Reception", "Rush").
            td = ty in ("Pass Reception", "Rush") and p.get("scoringPlay") \
                and (p.get("scoringType") or {}).get("name") == "touchdown" and not re.search(r"fumble|intercept", text, re.I)
            if ty == "Passing Touchdown" or (td and ty == "Pass Reception"):
                o["passTD"] += 1
            elif ty == "Rushing Touchdown" or (td and ty == "Rush"):
                o["rushTD"] += 1
            if ty.startswith("Sack"):
                o["sacks"] += 1  # sacks allowed by the offense
            if ty in TO_TYPES:
                o["to"] += 1
    for i in ids:
        rec, opp = teams[i], teams[ids[1] if i == ids[0] else ids[0]]
        rec["plays"] = full_plays[i] if have_pbp and full_plays[i] else rec["passAtt"] + rec["rushAtt"]
        rec.setdefault("passTD", 0.0); rec.setdefault("rushTD", 0.0); rec.setdefault("sacks", 0.0)
        if have_pbp:
            rec["sacks"] = float(full_sacks[ids[1] if i == ids[0] else ids[0]])  # sacks made = opponent's sacks taken
        if "sacksTaken" in opp:
            rec["sacks"] = opp["sacksTaken"]
        rec["h1x"] = h1[i] if have_pbp else None
    return {"id": comp["id"], "date": comp["date"], "neutral": comp.get("neutralSite", False), "teams": teams, "lg": lg,
            "line": game_lines(d)}


def game_lines(d):
    """The game's last pregame line from ESPN's pickcenter (DraftKings): home spread and total."""
    pc = (d.get("pickcenter") or [{}])[0]
    sp, tot = pc.get("spread"), pc.get("overUnder")
    home = None
    if sp is not None:
        sp = abs(float(sp))
        if (pc.get("homeTeamOdds") or {}).get("favorite"):
            home = -sp
        elif (pc.get("awayTeamOdds") or {}).get("favorite"):
            home = sp
        elif sp == 0:
            home = 0.0
    return {"spread": home, "total": float(tot) if tot not in (None, 0) else None}


def bet_record(gl, tid):
    """A team's record against the spread and on totals, from each finished game's closing DraftKings line.

    Each record is [wins, losses, pushes] (for totals: overs, unders, pushes). Neutral-site games count
    in the overall records only.
    """
    keys = ("ats", "atsHome", "atsAway", "atsFav", "atsDog", "ou", "ouHome", "ouAway")
    rec = {k: [0, 0, 0] for k in keys}
    margins, last = [], []
    for g in gl:
        ln = g.get("line") or {}
        me = g["teams"].get(tid)
        op = next((t for i, t in g["teams"].items() if i != tid), None)
        if not me or not op:
            continue
        where = None if g.get("neutral") else ("Home" if me["ha"] == "home" else "Away")
        if ln.get("spread") is not None:
            mine = ln["spread"] if me["ha"] == "home" else -ln["spread"]
            m = me["pts"] - op["pts"] + mine
            r = 0 if m > 0 else 1 if m < 0 else 2
            ks = ["ats"] + ([f"ats{where}"] if where else []) + (["atsFav"] if mine < 0 else ["atsDog"] if mine > 0 else [])
            for k in ks:
                rec[k][r] += 1
            margins.append(m)
            last.append("WLP"[r])
        if ln.get("total") is not None:
            t = me["pts"] + op["pts"] - ln["total"]
            r = 0 if t > 0 else 1 if t < 0 else 2
            for k in ["ou"] + ([f"ou{where}"] if where else []):
                rec[k][r] += 1
    rec["cover"] = round(sum(margins) / len(margins), 1) if margins else None
    rec["last5"] = last[-5:][::-1]
    return rec


def scoreboard(lg, week):
    q = f"groups=80&limit=400&week={week}&seasontype=2" if lg == "college-football" else f"week={week}&seasontype=2"
    return get(f"{BASE}/{lg}/scoreboard?{q}", f"sb_{lg}_{week}.json" if week < CUR[lg] else None)


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# --- per-team stat lines -------------------------------------------------------
# Each row: (label, key, higher_is_better). Offense/defense values come from the team's own perspective.
OFF = [("PPG", "pts", True), ("Pass YPG", "passY", True), ("Pass TD/G", "passTD", True), ("Rush YPG", "rushY", True),
       ("Rush TD/G", "rushTD", True), ("Sacks Allow/G", "sacksA", False)]
DEF = [("PPG/A", "o_pts", False), ("PassYPG/A", "o_passY", False), ("Pass TD/A", "o_passTD", False),
       ("RushYPG/A", "o_rushY", False), ("Rush TD/A", "o_rushTD", False), ("Sacks/G", "sacks", True)]
SIT = [("Off 3rd %", "d3pct", True), ("Def 3rd %", "o_d3pct", False), ("TO Margin", "toM", True),
       ("Penalties", "penY", False), ("Pace", "plays", True)]


def game_line(g, tid):
    """Flatten a game into full-game and first-half values from one team's perspective."""
    ids = list(g["teams"])
    me, op = g["teams"][tid], g["teams"][ids[1] if ids[0] == tid else ids[0]]
    full = {"pts": me["pts"], "passY": me["passY"], "passTD": me["passTD"], "rushY": me["rushY"], "rushTD": me["rushTD"],
            "sacksA": op["sacks"], "o_pts": op["pts"], "o_passY": op["passY"], "o_passTD": op["passTD"],
            "o_rushY": op["rushY"], "o_rushTD": op["rushTD"], "sacks": me["sacks"],
            "d3c": me["d3c"], "d3a": me["d3a"], "o_d3c": op["d3c"], "o_d3a": op["d3a"],
            "toM": op["to"] - me["to"], "penY": me["penY"], "plays": me["plays"]}
    half = None
    if me.get("h1x") and op.get("h1x"):
        a, b = me["h1x"], op["h1x"]
        half = {"pts": me["h1"], "passY": a["passY"], "passTD": a["passTD"], "rushY": a["rushY"], "rushTD": a["rushTD"],
                "sacksA": a["sacks"], "o_pts": op["h1"], "o_passY": b["passY"], "o_passTD": b["passTD"],
                "o_rushY": b["rushY"], "o_rushTD": b["rushTD"], "sacks": b["sacks"],
                "d3c": a["d3c"], "d3a": a["d3a"], "o_d3c": b["d3c"], "o_d3a": b["d3a"],
                "toM": b["to"] - a["to"],
                # Zero matched first-half penalty yards in a heavily penalized game means the play-by-play
                # names the team in a way we couldn't match: leave that game out rather than count a zero.
                "penY": None if a["penY"] == 0 and me["penY"] >= 30 else a["penY"], "plays": a["plays"]}
    return full, half, me["ha"], g["neutral"]


def summarize(lines):
    """Average a list of per-game dicts. 3rd-down % is the average of each game's rate (the way the
    Profit Hunt boards show it), so one game with many attempts doesn't outweigh the rest."""
    if not lines:
        return {}
    out = {}
    for k in ["pts", "passY", "passTD", "rushY", "rushTD", "sacksA", "o_pts", "o_passY", "o_passTD", "o_rushY",
              "o_rushTD", "sacks", "toM", "penY", "plays"]:
        out[k] = avg([l.get(k) for l in lines])
    out["d3pct"] = avg([100 * l["d3c"] / l["d3a"] if l["d3a"] else None for l in lines])
    out["o_d3pct"] = avg([100 * l["o_d3c"] / l["o_d3a"] if l["o_d3a"] else None for l in lines])
    return out


def rank_map(values, higher_better):
    """values: {team_id: value}. Returns {team_id: rank} with ties sharing the best rank."""
    vals = {k: v for k, v in values.items() if v is not None}
    order = sorted(vals.values(), reverse=higher_better)
    return {k: order.index(v) + 1 for k, v in vals.items()}


HFA = {"cfb": 2.5, "nfl": 1.5, "nba": 2.5, "cbb": 3.5}  # home edge in points (standard values)


def fit_ratings(games, lg, universe=None):
    """Ridge least squares: points = mu + off[team] - def[opp], with a fixed home edge.

    Opponents outside the universe (FCS teams on an FBS schedule) share one pooled rating,
    so a cupcake win counts as beating a weak team instead of inflating home-field advantage.
    """
    key = lambda t: t if universe is None or t in universe else "_other"
    teams = sorted({key(t) for g in games for t in g["teams"]})
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    hfa = HFA.get(lg, 2.0)
    rows, y = [], []
    for g in games:
        ids = list(g["teams"])
        for a, b in [(ids[0], ids[1]), (ids[1], ids[0])]:
            r = np.zeros(2 * n + 1)
            r[0] = 1
            r[1 + idx[key(a)]] = 1
            r[1 + n + idx[key(b)]] = -1
            home = 0 if g["neutral"] else (0.5 if g["teams"][a]["ha"] == "home" else -0.5)
            rows.append(r); y.append(g["teams"][a]["pts"] - home * hfa)
    X, y = np.array(rows), np.array(y)
    lam = {"nfl": 3.0, "cfb": 2.0, "nba": 10.0, "cbb": 4.0}.get(lg, 2.0)
    reg = np.full(2 * n + 1, lam); reg[0] = 1e-6
    beta = np.linalg.solve(X.T @ X + np.diag(reg), X.T @ y)
    rat = {t: (beta[1 + i], beta[1 + n + i]) for t, i in idx.items()}
    return beta[0], hfa, {t: rat[key(t)] for g in games for t in g["teams"]}


TRUST_K = {"cfb": 4, "nfl": 6, "nba": 25, "cbb": 10}  # games of evidence the model needs to count half


def trust(n, key):
    """Share of weight the model gets against the betting market, growing with games played."""
    return n / (n + TRUST_K.get(key, 6))


def sos_val(gl, tid, net, n=5):
    """Average rating (points per game vs average) of a team's last n opponents; unrated opponents count as the weakest."""
    if not gl or not net:
        return None
    floor = min(net.values())
    opp = [net.get(next((k for k in g["teams"] if k != tid), None), floor) for g in gl[-n:]]
    return round(sum(opp) / len(opp), 2) if opp else None


def american(ml):
    return None if ml in (None, 0) else (1 + ml / 100 if ml > 0 else 1 + 100 / -ml)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fmt_ml(v):
    return "—" if v is None else (f"+{int(v)}" if v > 0 else str(int(v)))


def fmt_spread(v):
    if v is None:
        return "—"
    if v == 0:
        return "PK"
    return f"+{v:g}" if v > 0 else f"{v:g}"


def conf(score):
    return round(min(10.0, max(0.0, score)), 1)


def call(score, text):
    return text if score >= 4 else "Pass"


def label(score, text):
    return ("Play " if score >= 7 else "Lean ") + text if score >= 4 else "Pass"


# --- NFL player props ------------------------------------------------------------
# key: (label, value from a player game, team stat the opponent allows, higher allowed = easier matchup,
#       minimum per-game average to list the player)
PROP_CATS = [
    ("Passing", lambda p: p.get("passYds"), "o_passY", True, 120, lambda p: p.get("passAtt", 0) >= 10),
    ("Rushing", lambda p: p.get("rushYds"), "o_rushY", True, 25, lambda p: p.get("car", 0) >= 4),
    ("Receiving", lambda p: p.get("recYds"), "o_passY", True, 25, lambda p: "rec" in p),
    ("Receptions", lambda p: p.get("rec"), "o_passY", True, 2.5, lambda p: "rec" in p),
    ("Rush + Rec", lambda p: (p.get("rushYds", 0) + p.get("recYds", 0)) if ("rushYds" in p or "recYds" in p) else None,
     "o_totY", True, 45, lambda p: p.get("car", 0) >= 4 or p.get("rec", 0) >= 2),
    ("Touchdowns", lambda p: (p.get("rushTD", 0) + p.get("recTD", 0)) if ("rushYds" in p or "recYds" in p) else None,
     "o_td", True, 0.0, lambda p: p.get("car", 0) >= 1 or p.get("rec", 0) >= 1),  # every active rusher / receiver
]
# ESPN prop market for each category (DraftKings lines via ESPN's odds feed).
PROP_MARKETS = {
    "Passing": "Total Passing Yards (incl. overtime)", "Rushing": "Total Rushing Yards (incl. overtime)",
    "Receiving": "Total Receiving Yards (incl. overtime)", "Receptions": "Total Receptions (incl. overtime)",
    "Rush + Rec": "Total Rushing Plus Receiving Yards (incl. overtime)", "Touchdowns": "Anytime Touchdown Scorer",
    "Sacks": "Total Sacks (incl. overtime)", "Tackles + Ast": "Total Tackles Plus Assists (incl. overtime)",
    "FG Made": "Total Field Goals Made (incl. overtime)",
}
COUNT_CATS = {"Touchdowns", "Sacks"}  # modeled as Poisson counts
SD_FLOOR = {"Passing": 35, "Rushing": 15, "Receiving": 15, "Receptions": 1.2, "Rush + Rec": 18, "Tackles + Ast": 1.5,
            "FG Made": 0.8}
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues"


def prop_lines(lg, eid):
    """{(market name, athlete id): line} for one game, from the first sportsbook ESPN lists."""
    odds = get(f"{CORE}/{lg}/events/{eid}/competitions/{eid}/odds?lang=en&region=us")
    items = (odds or {}).get("items") or []
    if not items or "propBets" not in items[0]:
        return {}
    url = items[0]["propBets"]["$ref"].replace("http://", "https://")
    out, page = {}, 1
    while True:
        d = get(f"{url}&limit=1000&page={page}")
        for it in (d or {}).get("items", []):
            name = (it.get("type") or {}).get("name")
            aid = ((it.get("athlete") or {}).get("$ref") or "").split("/athletes/")[-1].split("?")[0]
            if not aid or name not in PROP_MARKETS.values():
                continue
            tgt = ((it.get("current") or {}).get("target") or {}).get("value")
            opn = ((it.get("open") or {}).get("target") or {}).get("value")
            out[(name, aid)] = (0.5, None) if name == "Anytime Touchdown Scorer" else (tgt, opn)
        if not d or page >= d.get("pageCount", 1):
            break
        page += 1
    return out


def prop_score(cat, proj, line, hist, count_cats=COUNT_CATS, sd_floor=SD_FLOOR):
    """Blend the model's over probability with the last-5 hit rate; 0 = coin flip, 10 = strongest."""
    if line is None:
        return None, None, None
    if cat in count_cats:
        lam = max(proj, 0.01)
        k = math.floor(line)  # P(X > line) for a Poisson count
        p_over = 1 - sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k + 1))
    else:
        m = sum(hist) / len(hist)
        sd = math.sqrt(sum((x - m) ** 2 for x in hist) / max(1, len(hist) - 1))
        sd = max(sd, sd_floor.get(cat, 1.0), 0.15 * abs(proj))
        p_over = 1 - phi((line - proj) / sd)
    l5 = hist[-5:]
    hit = sum(1 for x in l5 if x > line) / len(l5)
    p = 0.7 * p_over + 0.3 * hit
    if cat == "Touchdowns":  # anytime TD props are effectively "yes" only
        return round(min(10.0, max(0.0, p - 0.5) * 20), 1), "Over", round(p, 3)
    side = "Over" if p >= 0.5 else "Under"
    return round(min(10.0, abs(p - 0.5) * 20), 1), side, round(p if p >= 0.5 else 1 - p, 3)


ZERO_FILL = {"Rushing", "Receiving", "Receptions", "Rush + Rec", "Touchdowns"}
OUT_STATUSES = {"out", "doubtful", "injured reserve", "suspended", "physically unable to perform"}


PLAYER_LOGS = {"fb": {}, "bb": {}}  # sport -> {player id: last 5 games, newest first}
LOG_FIELDS = {"fb": ["cmp", "passAtt", "passYds", "passTD", "int", "car", "rushYds", "rushTD", "rushLong", "rec", "tgt",
                     "recYds", "recTD", "recLong", "fg"],
              "bb": ["min", "pts", "reb", "oreb", "dreb", "ast", "tov", "fgm", "fga", "tpm", "tpa", "fta", "stl", "blk"]}


def player_log(sport, gl, tid, pid, n=5):
    """The player's last n games (newest first) for the prop table's game log: date, opponent, then LOG_FIELDS."""
    out = PLAYER_LOGS[sport]
    if pid in out:
        return
    rows = []
    for g in reversed(gl):
        q = g["teams"][tid].get("players", {}).get(pid)
        if not q:
            continue
        opp = next((t for k, t in g["teams"].items() if k != tid), {})
        d = datetime.fromisoformat(g["date"].replace("Z", "+00:00")).astimezone(ET)
        rows.append([f"{d.month}/{d.day}", ("@ " if g["teams"][tid].get("ha") == "away" else "vs ") + opp.get("abbr", "?")]
                    + [(int(q[f]) if float(q[f]).is_integer() else round(q[f], 1)) if isinstance(q.get(f), (int, float)) else None
                       for f in LOG_FIELDS[sport]])
        if len(rows) >= n:
            break
    out[pid] = rows


def build_props(game, opp_id, team, opp, per_team, agg, injured, lines):
    """Rows for one team's players in one upcoming game."""
    tid = team["id"]
    gl = per_team.get(tid, [])
    if not gl:
        return []
    # Anyone who played in the last 3 games (not just the last one), so a player back from a missed game still gets lines.
    latest = {}
    for gg in gl[-3:]:
        latest.update(gg["teams"][tid].get("players", {}))
    vac = vacated(gl, tid, injured)
    n = len(agg)
    rows = []
    for label, val, opp_key, easy_high, min_avg, active in PROP_CATS:
        allowed = {t: (a["season"].get("o_passY", 0) + a["season"].get("o_rushY", 0)) if opp_key == "o_totY" else
                   (a["season"].get("o_passTD", 0) + a["season"].get("o_rushTD", 0)) if opp_key == "o_td" else
                   a["season"].get(opp_key) for t, a in agg.items()}
        # Rank 1 = the toughest matchup (allows the least), as on the video's board.
        rk = rank_map(allowed, not easy_high)
        orank = rk.get(opp_id)
        league = sum(v for v in allowed.values() if v is not None) / max(1, len([v for v in allowed.values() if v is not None]))
        factor = min(1.2, max(0.8, (allowed.get(opp_id) or league) / league)) if league else 1.0
        for pid, p in latest.items():
            if pid in injured or not active(p):
                continue
            hist = []
            for g in gl:
                q = g["teams"][tid].get("players", {}).get(pid)
                v = val(q) if q else None
                # ESPN leaves a player out of a stat table when he records nothing in it; if he shows up
                # elsewhere in the box score he played, so count the skill-position stats as zero.
                if v is None and q and label in ZERO_FILL:
                    v = 0.0
                if v is not None:
                    hist.append(v)
            posted = lines.get((PROP_MARKETS[label], pid))
            # Three games to model a player, unless the book has posted his line (a new starter or a player
            # back from injury): then show him, and the small-sample warning keeps him from being a full Take.
            if len(hist) < (1 if posted and posted[0] is not None else 3):
                continue
            l5 = hist[-5:]
            avg5, season = sum(l5) / len(l5), sum(hist) / len(hist)
            if season < min_avg:
                continue
            med5 = sorted(l5)[len(l5) // 2] if len(l5) % 2 else sum(sorted(l5)[len(l5) // 2 - 1:len(l5) // 2 + 1]) / 2
            proj = (0.5 * med5 + 0.5 * season) * factor  # median of the last 5, so one big game can't carry the projection
            line, opn = posted or (None, None)
            if label == "Touchdowns" and line is None:
                line = 0.5  # anytime TD is always 0.5; the price comes from Kalshi when it lists the player
            score, side, prob = prop_score(label, proj, line, hist)
            player_log("fb", gl, tid, pid)
            rows.append({"pid": pid, "line": line, "open": opn, "score": score, "side": side, "prob": prob,
                         "hits": sum(1 for x in l5 if line is not None and x > line) if line is not None else None,"cat": label, "player": p["name"], "full": p.get("full"), "team": team["abbr"],
                         "opp": opp["abbr"], "oppName": opp["name"], "home": team["ha"] == "home",
                         "oppRank": orank, "matchup": round(10 * (orank - 1) / (n - 1), 1) if orank else None,
                         "l5": [round(x, 1) for x in reversed(l5)], "avg5": round(avg5, 1), "season": round(season, 1),
                         "games": len(hist), "proj": round(proj, 1), "game": game, "_hist": hist,
                         **(carry_load(gl, tid, pid) if label in ("Rushing", "Rush + Rec") else {}),
                         **({"vacated": vac[VAC_KIND[label]]} if VAC_KIND.get(label) and vac.get(VAC_KIND[label]) and pid not in vac[VAC_KIND[label]]["ids"] else {})})
    return rows


def load_returning():
    """build/returning.json: players back from IR or a long absence. Their teammates' recent numbers were built
    while they were out, so that volume is about to shrink (the reverse of a vacated role)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "returning.json")) as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    today = datetime.now(ET).strftime("%Y-%m-%d")
    return [r for r in rows if (r.get("until") or "9999") >= today]


def mark_returning(props, lg):
    rows = [r for r in load_returning() if r.get("league") == lg]
    if not rows:
        return
    qbs = {p["pid"] for p in props if p["cat"] == "Passing"}  # a QB's rushing doesn't share a backfield's carries
    backs = {p["pid"] for p in props if p["cat"] == "Rushing" and p.get("line") is not None}
    for r in rows:
        cats = ("Rushing", "Rush + Rec") if r.get("kind", "carries") == "carries" else ("Receiving", "Receptions")
        for p in props:
            if p["team"] == r["team"] and p["cat"] in cats and p["pid"] not in qbs and (cats[0] != "Rushing" or p["pid"] in backs) and (p.get("full") or p["player"]) not in r["names"]:
                p["returning"] = {"names": r["names"], "kind": r.get("kind", "carries")}


VAC_KIND = {"Rushing": "carries", "Rush + Rec": "carries", "Receiving": "catches", "Receptions": "catches"}
VAC_MIN = 0.15  # a teammate's share of carries or catches that counts as real volume


def vacated(gl, tid, injured):
    """Share of the team's carries and catches (last 3 games) belonging to players now out or doubtful.
    That volume goes to whoever is left, so it helps their overs and hurts their unders."""
    out = {}
    for kind, stat in (("carries", "car"), ("catches", "rec")):
        tot, mine, names = 0.0, {}, {}
        for gg in gl[-3:]:
            for pid, q in (gg["teams"][tid].get("players") or {}).items():
                v = q.get(stat) or 0
                tot += v
                mine[pid] = mine.get(pid, 0) + v
                names[pid] = q.get("name")
        if not tot:
            continue
        gone = [(pid, mine[pid] / tot) for pid in mine if pid in injured and mine[pid] / tot >= VAC_MIN]
        if gone:
            out[kind] = {"share": round(sum(sh for _, sh in gone), 2), "names": [names[p] for p, _ in gone], "ids": [p for p, _ in gone]}
    return out


def carry_load(gl, tid, pid):
    """Carries per game over the last 5 and yards per carry on the season (used in scoring, not shown)."""
    car, yds = [], []
    for g in gl:
        q = g["teams"][tid].get("players", {}).get(pid)
        if q and "car" in q:
            car.append(q.get("car") or 0)
            yds.append(q.get("rushYds") or 0)
    if not car or sum(car) < 10:
        return {}
    return {"_car5": sum(car[-5:]) / len(car[-5:]), "_ypc": sum(yds) / max(1, sum(car))}


# --- market data: opening lines (DraftKings via ESPN) and Kalshi prices ------------
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CORE_PATH = {"cfb": "football/leagues/college-football", "nfl": "football/leagues/nfl",
             "nba": "basketball/leagues/nba", "cbb": "basketball/leagues/mens-college-basketball"}
KSERIES = {
    "nfl": ("KXNFL", {"Passing": "KXNFLPASSYDS", "Rushing": "KXNFLRSHYDS", "Receiving": "KXNFLRECYDS", "Receptions": "KXNFLREC",
                      "Rush + Rec": "KXNFLRRYDS", "Touchdowns": "KXNFLTD", "Sacks": "KXNFLSACK", "FG Made": "KXNFLFG"}),
    "cfb": ("KXNCAAF", {}),
    "nba": ("KXNBA", {"Points": "KXNBAPTS", "Rebounds": "KXNBAREB", "Assists": "KXNBAAST", "PRA": "KXNBAPRA", "Pts+Reb": "KXNBAPR",
                      "Pts+Ast": "KXNBAPA", "Reb+Ast": "KXNBARA", "3PT Made": "KXNBA3PT", "Blk+Stl": "KXNBASTOCKS"}),
    "cbb": ("KXNCAAMB", {}),
}


def norm(x):
    x = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", (x or "").lower())
    return re.sub(r"[^a-z0-9]", "", x)


def kalshi_paged(path, key):
    out, cursor = [], ""
    for _ in range(20):
        time.sleep(0.4)  # stay under Kalshi's public rate limit
        d = get(f"{KALSHI}/{path}&limit={1000 if key == 'markets' else 200}" + (f"&cursor={cursor}" if cursor else ""))
        if not d:
            break
        out += d.get(key) or []
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def kmid(m):
    """Midpoint of the yes bid/ask, or None when the market is too thin to trust."""
    try:
        b, a = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
    except ValueError:
        return None
    if a <= 0 or a - b > 0.2:
        return None
    return (a + b) / 2


def ladder_p(markets, line):
    """P(stat > line) read off a Kalshi strike ladder ("X+" markets), interpolating between rungs."""
    pts = sorted((m["floor_strike"], kmid(m)) for m in markets if m.get("floor_strike") is not None and kmid(m) is not None)
    if not pts or line is None:
        return None
    clean, lo = [], 1.0  # probabilities must fall as the strike rises
    for k, p in pts:
        lo = min(lo, p); clean.append((k, lo))
    if line < clean[0][0] - 1 or line > clean[-1][0] + 1:
        return None
    if line <= clean[0][0]:
        return clean[0][1]
    if line >= clean[-1][0]:
        return clean[-1][1]
    for (k1, p1), (k2, p2) in zip(clean, clean[1:]):
        if k1 <= line <= k2:
            return p1 + (p2 - p1) * (line - k1) / (k2 - k1) if k2 > k1 else p1


def fair(p):
    if p is None or p <= 0.01 or p >= 0.99:
        return None
    return fmt_ml(round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p))


def backfill_line(key, eid, pc):
    """For a finished game, ESPN's final line is usually the closing line, but sometimes it is an in-game line
    (off-market juice such as +140 on a spread). In that case use the opening line, which is always pregame."""
    def pn(v):
        try:
            return float(str(v).replace("+", ""))
        except (TypeError, ValueError):
            return None
    odds = [pn(((pc.get("awayTeamOdds") or {}).get("spreadOdds"))), pn(((pc.get("homeTeamOdds") or {}).get("spreadOdds"))),
            pn((pc.get("overOdds"))), pn((pc.get("underOdds")))]
    if all(o is not None and -140 <= o <= 120 and abs(o) >= 100 for o in odds) and pc.get("spread") is not None:
        return {**pc, "provider": {"name": "DraftKings close"}}
    it = ((get(f"https://sports.core.api.espn.com/v2/sports/{CORE_PATH[key]}/events/{eid}/competitions/{eid}/odds?lang=en&region=us") or {})
          .get("items") or [{}])[0]
    o = lambda *ks: _dig(it, *ks)
    try:
        hs = float(str(o("homeTeamOdds", "open", "pointSpread", "american")).replace("PK", "0"))
        tot = float(o("open", "total", "american"))
    except (TypeError, ValueError):
        return {}
    ml = lambda side: pn(o(side, "open", "moneyLine", "american"))
    jc = lambda *ks: pn(o(*ks)) or -110
    return {"provider": {"name": "DraftKings open"}, "spread": hs, "overUnder": tot,
            "awayTeamOdds": {"moneyLine": ml("awayTeamOdds"), "spreadOdds": jc("awayTeamOdds", "open", "spread", "american")},
            "homeTeamOdds": {"moneyLine": ml("homeTeamOdds"), "spreadOdds": jc("homeTeamOdds", "open", "spread", "american")},
            "overOdds": jc("open", "over", "american"), "underOdds": jc("open", "under", "american")}


def enrich(key, slate):
    """Attach opening lines (DraftKings via ESPN) and Kalshi fair prices to games and props."""
    for g in slate:
        it = ((get(f"https://sports.core.api.espn.com/v2/sports/{CORE_PATH[key]}/events/{g['id']}/competitions/{g['id']}/odds?lang=en&region=us") or {})
              .get("items") or [{}])[0]
        am = lambda d, *ks: next((v for v in [_dig(d, *ks)] if v not in (None, "")), None)
        g["lines"]["open"] = {
            "awaySpread": am(it, "awayTeamOdds", "open", "pointSpread", "american"), "homeSpread": am(it, "homeTeamOdds", "open", "pointSpread", "american"),
            "awayML": am(it, "awayTeamOdds", "open", "moneyLine", "american"), "homeML": am(it, "homeTeamOdds", "open", "moneyLine", "american"),
            "total": am(it, "open", "total", "american")}
    prefix, prop_series = KSERIES[key]
    events = kalshi_paged(f"events?series_ticker={prefix}GAME&status=open", "events")
    if not events:
        for g in slate:
            for pr in g.get("props") or []:
                rescore_prop(pr, key)
                for k in ("_hist", "_car5", "_ypc"):
                    pr.pop(k, None)
        return
    series = {"GAME": f"{prefix}GAME", "SPREAD": f"{prefix}SPREAD", "TOTAL": f"{prefix}TOTAL", **prop_series}
    by = {}
    for name, tick in series.items():
        for m in kalshi_paged(f"markets?series_ticker={tick}&status=open", "markets"):
            suf = m["event_ticker"].split("-", 1)[-1]
            by.setdefault(name, {}).setdefault(suf, []).append(m)

    def names(t):
        out = {norm(x) for x in [t.get("loc"), t.get("name"), t.get("full"), t.get("abbr")] if x}
        if t.get("loc") in ("LA", "Los Angeles") and t.get("name"):
            out.add(norm("Los Angeles " + t["name"][0]))  # Kalshi: "Los Angeles C" / "Los Angeles L"
        return out

    def same(label, t):
        n = norm(label)
        return bool(n) and any(n == x or (len(n) > 3 and (x.startswith(n) or n.startswith(x))) for x in names(t))

    matched = 0
    for g in slate:
        if g.get("state") != "pre":
            continue
        now = datetime.now(ET)
        when = datetime.strptime(g["date"].split(" · ")[0] + f" {now.year}", "%b %d %Y")
        if when.month < 6 and now.month > 8:  # a January game seen from December
            when = when.replace(year=now.year + 1)
        codes = {(when + timedelta(days=d)).strftime("%y%b%d").upper() for d in (-1, 0, 1)}
        ev = None
        for e in events:
            suf = e["event_ticker"].split("-", 1)[-1]
            if suf[:7] not in codes:
                continue
            a, _, b = (e.get("title") or "").partition(" vs ")
            if (same(a, g["away"]) and same(b, g["home"])) or (same(a, g["home"]) and same(b, g["away"])):
                ev = suf
                break
        if not ev:
            continue
        matched += 1
        k = {}
        side_of = lambda label: "away" if same(label, g["away"]) else "home" if same(label, g["home"]) else None
        ml = {}
        for m in by.get("GAME", {}).get(ev, []):
            sd, p = side_of((m.get("title") or "").replace(" wins", "")), kmid(m)
            if sd and p is not None:
                ml[sd] = p
        if len(ml) == 2 and sum(ml.values()) > 0:
            tot = sum(ml.values())
            k["ml"] = {sd: {"p": round(p / tot, 3), "odds": fair(p / tot)} for sd, p in ml.items()}
            if key in ("nba", "cbb") and g["lines"].get("spreadNum") is None and 0.02 < k["ml"]["home"]["p"] < 0.98:
                # No sportsbook line yet: Kalshi's winner market posts first. Its win chance implies a spread
                # (margin ~ normal with a 12-point spread in the NBA, 11 in college), and its prices stand in for the
                # moneyline until a book posts. Totals wait for a book.
                sig = 12.0 if key == "nba" else 11.0
                mu = sig * NormalDist().inv_cdf(k["ml"]["home"]["p"])  # home's expected margin
                hs = -round(mu * 2) / 2
                if hs == 0:
                    hs = -0.5 if mu > 0 else 0.5
                ln = g["lines"]
                ln.update({"spreadNum": hs, "homeSpread": fmt_spread(hs), "awaySpread": fmt_spread(-hs),
                           "homeML": k["ml"]["home"]["odds"], "awayML": k["ml"]["away"]["odds"],
                           "homeSpreadOdds": "-110", "awaySpreadOdds": "-110", "book": "Kalshi (implied)", "filled": True})
        ladders = {}
        for m in by.get("SPREAD", {}).get(ev, []):
            sd = side_of((m.get("title") or "").split(" wins by")[0])
            if sd:
                ladders.setdefault(sd, []).append(m)
        hs = g["lines"].get("spreadNum")
        if hs is not None and ladders:
            # home covers when (home - away) > -hs
            need = -hs
            p_home = ladder_p(ladders.get("home", []), need) if need >= 0 else (
                1 - ladder_p(ladders.get("away", []), -need) if ladder_p(ladders.get("away", []), -need) is not None else None)
            if p_home is not None:
                k["spread"] = {"home": {"p": round(p_home, 3), "odds": fair(p_home)}, "away": {"p": round(1 - p_home, 3), "odds": fair(1 - p_home)}}
        tl = g["lines"].get("totalNum")
        po = ladder_p(by.get("TOTAL", {}).get(ev, []), tl)
        if po is not None:
            k["total"] = {"over": {"p": round(po, 3), "odds": fair(po)}, "under": {"p": round(1 - po, 3), "odds": fair(1 - po)}}
        if k:
            g["kalshi"] = k
        # player props: ladder for the player at DraftKings' line
        for pr in g.get("props") or []:
            ms = [m for m in by.get(pr["cat"], {}).get(ev, []) if norm((m.get("title") or "").split(":")[0]) == norm(pr.get("full") or pr["player"])]
            if not ms:
                continue
            if pr.get("line") is None and pr.get("_hist"):
                # No DraftKings line: use Kalshi's own line, the "X+" rung nearest a coin flip (over X - 0.5).
                rungs = [(m["floor_strike"], kmid(m)) for m in ms if m.get("floor_strike") is not None and kmid(m) is not None]
                if rungs:
                    k_line = min(rungs, key=lambda r: abs(r[1] - 0.5))[0] - 0.5
                    if k_line > 0:
                        args = (BB_COUNT, BB_FLOOR) if key in ("nba", "cbb") else ()
                        pr["score"], pr["side"], pr["prob"] = prop_score(pr["cat"], pr["proj"], k_line, pr["_hist"], *args)
                        pr["line"], pr["lineSrc"] = k_line, "Kalshi"
                        pr["hits"] = sum(1 for x in pr["_hist"][-5:] if x > k_line)
            if pr.get("line") is None:
                continue
            po = ladder_p(ms, pr["line"])
            if po is None:
                continue
            ps = po if pr.get("side") != "Under" else 1 - po
            pr["kalshiP"], pr["kalshiOdds"] = round(ps, 3), fair(ps)
            pr["_ms"] = ms
    print(key, "kalshi matched", matched, "of", len(slate), file=sys.stderr)
    for g in slate:
        for pr in g.get("props") or []:
            rescore_prop(pr, key)
            if key in ("nba", "cbb"):
                kalshi_alt_line(pr, key)
            for k in ("_hist", "_car5", "_ypc", "_ms"):
                pr.pop(k, None)


def kalshi_alt_line(pr, key):
    """No value at DraftKings' line: look along the player's Kalshi ladder (1-3 away) for a line where our read
    still makes it a bet and Kalshi's price leaves a real edge. If one exists, the prop moves to that Kalshi line."""
    ms, hist = pr.get("_ms"), pr.get("_hist")
    if not ms or not hist or pr.get("kalshiP") is None or (pr.get("edge") or 0) >= PROP_EDGE_MIN or pr.get("line") is None:
        return
    base, best = pr["line"], None
    steps = (1, 2) if pr["cat"] in BB_COUNT or pr["cat"] in ("Assists", "Rebounds") else (1, 2, 3)
    for d in [x for st in steps for x in (-st, st)]:
        L = base + d
        if L <= 0:
            continue
        po = ladder_p(ms, L)
        if po is None:
            continue
        sc, side, prob = prop_score(pr["cat"], pr["proj"], L, hist, BB_COUNT, BB_FLOOR)
        if sc is None:
            continue
        kp = po if side != "Under" else 1 - po
        if not 0.05 < kp <= 0.6:
            continue  # no long shots, and nothing priced -150 or heavier
        t = dict(pr, line=L, side=side, prob=prob, score=sc, kalshiP=round(kp, 3), kalshiOdds=fair(kp))
        t["l5"] = pr.get("l5")
        rescore_prop(t, key)
        bar = NBA_PROP_BAR if (pr.get("mpg") or 0) >= BB_PROP_MPG else BB_PROP_BAR_LOWMIN
        if ((t.get("edge") or 0) >= PROP_EDGE_MIN and (t.get("score") or 0) >= bar and (pr["cat"], side) in BB_GOOD_PROPS
                and (best is None or t["score"] > best["score"])):
            best = t
    if best:
        best["hits"] = sum(1 for x in hist[-5:] if x > best["line"])
        best["dkLine"], best["lineSrc"] = base, "Kalshi"
        pr.update(best)


def rescore_prop(pr, key=None):
    """Final prop score: how far a market-anchored blend beats the market price.

    The market price is Kalshi's when there is one, otherwise a coin flip at DraftKings' line
    (anytime TDs without Kalshi stay unscored: their fair price varies too much). The model's
    weight shrinks as the line moves away from the player's recent form, because a line far from
    form that the market agrees with usually reflects injury or role news the box scores lack.
    10 means the blend beats the market by 12 percentage points or more.
    """
    if pr.get("line") is None or pr.get("prob") is None:
        return
    market = pr.get("kalshiP")
    if market is None:
        if pr["cat"] == "Touchdowns":
            pr["score"], pr["edge"] = None, None
            return
        market = 0.5
    form = 0.5 * pr["avg5"] + 0.5 * pr["season"]
    gap = abs(form - pr["line"]) / max(pr["line"], 1.0)
    if pr["cat"] == "FG Made":
        gap /= 3  # kicker volume swings a lot game to game, so a line well below the average is normal
    pr["flag"] = gap > 0.35 and pr["cat"] != "Touchdowns" and (key not in ("nba", "cbb") or abs(form - pr["line"]) >= 2.0)
    gw = gap
    if key in ("nba", "cbb") and pr["cat"] in ("3PT Made", "Assists", "Rebounds"):
        # Small-count lines (o1.5 threes, o2.5 boards) look "far" from form in percentage terms when they're only a
        # board or two off, which zeroed the model's weight and hid real Kalshi value. Measure those against 4.
        gw = abs(form - pr["line"]) / max(pr["line"], 4.0)
    w = 0.35 * max(0.0, 1 - gw / 0.5)
    blend = w * pr["prob"] + (1 - w) * market
    pr["edge"] = round(blend - market, 3)
    if pr["cat"] == "Touchdowns":
        # Anytime TD: rank how likely the player is to score (blended chance plus the matchup), like the
        # Profit Hunt sheets; whether the price is worth it shows as Edge and decides Take vs Fade.
        # The score leans on his own scoring data (75%); Kalshi's price only nudges it (it was 65%+ of the read).
        likely = max(0.0, min(10.0, ((0.75 * pr["prob"] + 0.25 * market) - 0.05) / 0.6 * 10))
        pr["score"] = round(max(0.0, min(10.0, 0.7 * likely + 0.3 * (pr.get("matchup") or 5))), 1)
        return
    # Score = how strongly the player's data backs the pick (like the Profit Hunt sheets): the model's chance
    # for the side, his last-5 hit rate at this line and the matchup. Edge (vs Kalshi) then decides whether the
    # price is worth it: a strong data score the market already agrees with is a Fade, not a Take.
    under = pr.get("side") == "Under"
    l5 = pr.get("l5") or []
    # Hit rate weighted toward the latest games (l5 is newest first): last game 5x, five games ago 1x.
    wts = [5, 4, 3, 2, 1][:len(l5)]
    hit = (sum(w for w, v in zip(wts, l5) if (v < pr["line"] if under else v > pr["line"])) / sum(wts)) if l5 else 0.5
    m = pr.get("matchup")
    m = 5.0 if m is None else (10 - m if under else m)
    if key in ("nba", "cbb") and l5:
        # Basketball: how often he has cleared this line lately (newest games count most) and by how much his
        # last-5 average clears it, with the matchup as a light touch (fit to the Profit Hunt prop sheets).
        avg = sorted(l5)[len(l5) // 2] if len(l5) % 2 else sum(sorted(l5)[len(l5) // 2 - 1:len(l5) // 2 + 1]) / 2
        # The middle game of the last 5 (median), so one big night can't carry the read (JD: "that 24 is doing a lot
        # of heavy lifting"). Lines already move after a hot week, so the cushion uses half last-5, half season form.
        avg = 0.5 * avg + 0.5 * (pr.get("season") if pr.get("season") is not None else avg)
        cushion = (pr["line"] - avg if under else avg - pr["line"]) / max(pr["line"], 1.0)
        cushion = max(-1.0, min(1.0, cushion))
        if not under:
            hit_adj = OVER_TAX  # books shade over prices (the public bets overs), so an over needs a bit more
        else:
            hit_adj = 0.0
        vac_bump = 0.0
        if pr.get("bbVac"):  # a teammate's shots, boards or assists are up for grabs (JD: Durant out helps Miller's rebounds)
            vac_bump = min(0.8, 0.2 + 2 * pr["bbVac"]["share"]) * (-1 if under else 1)
        if pr["cat"] == "3PT Made":
            # Threes swing on shooting luck, so the matchup (how well the defense has guarded the arc lately) counts
            # far more and the cushion less (fit to the Profit Hunt 3PT sheet).
            pr["score"] = round(max(0.0, min(10.0, 1.875 + 2.81 * hit + 2.6 * cushion + 0.4 * m - hit_adj + vac_bump)), 1)
        else:
            # The matchup counts for real (Nov 4, closer to JD's sheets): 0.25 a point on its 0-10 scale, with the base
            # lowered so an average matchup (5) scores about what it did before.
            pr["score"] = round(max(0.0, min(10.0, 1.8 + 3.4 * hit + 2.9 * cushion + BB_PROP_MATCHUP_W * m - hit_adj + vac_bump)), 1)
        return
    s_prob = max(0.0, min(10.0, (pr["prob"] - 0.5) / 0.3 * 10))
    if key in ("nfl", "cfb"):
        # Football: the recent hit rate (newest games count most) carries more, so a maxed-out projection alone can't make a play.
        sc = 0.45 * s_prob + 0.35 * 10 * hit + 0.2 * m
        if pr["cat"] in ("Rushing", "Rush + Rec") and pr.get("_car5") and pr.get("_ypc", 0) > 0:
            # Workload: more carries means more yards. Compare his recent carries with what this line needs at his
            # yards per carry (for rush + rec, the rushing share only matters roughly, so the nudge is smaller).
            need = pr["line"] / pr["_ypc"] if pr["cat"] == "Rushing" else None
            if need:
                ratio = pr["_car5"] / max(need, 1.0)
                nudge = 0.4 if ratio >= 1.15 else -0.5 if ratio <= 0.9 else 0.0
                sc += -nudge if under else nudge
            else:
                trend = pr["_car5"]  # rush + rec: only a light-volume back (under 10 carries) gets trimmed on overs
                if trend < 10:
                    sc += 0.3 if under else -0.3
        vc = pr.get("vacated")
        if vc:  # a teammate's volume is up for grabs: more touches for this player
            bump = min(1.0, 0.5 + vc["share"])
            sc += -bump if under else bump
        if pr.get("returning"):  # teammates back from injury take part of the volume his recent games were built on
            sc += 1.0 if under else -1.0
        if not under:
            sc -= OVER_TAX
        pr["score"] = round(max(0.0, min(10.0, sc)), 1)
        return
    pr["score"] = round(max(0.0, min(10.0, 0.6 * s_prob + 0.2 * 10 * hit + 0.2 * m)), 1)


def _dig(d, *ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


FIN = {}  # league -> espn game id -> final scores (and player box lines for props)


def record_finals(key, games):
    out = FIN.setdefault(key, {})
    for g in games:
        side = {r["ha"]: r for r in g["teams"].values()}
        if "home" not in side or "away" not in side:
            continue
        players = {}
        for r in g["teams"].values():
            players.update(r.get("players") or {})
        out[g["id"]] = {"away": side["away"]["pts"], "home": side["home"]["pts"], "players": players}


KEY_POS = {"QB": 3, "RB": 1, "WR": 1, "TE": 1, "OT": 1, "T": 1, "C": 1, "G": 1, "CB": 1, "S": 1, "LB": 1, "DE": 1, "DT": 1, "EDGE": 1}


# --- full injury lists -----------------------------------------------------------------------------------
# A game summary lists at most five injured players per team, so the league-wide injury feed (every team's
# full report, including game-day inactives) is merged in. build/injury_overrides.json adds players known to
# be out that the feed hasn't caught up with yet: [{"league": "nfl", "team": "MIN", "name": "Aaron Jones Sr.",
# "status": "Out", "until": "2026-11-02"}].
_LEAGUE_INJ = {}
STATUS_MAP = {"day-to-day": "Questionable"}


def league_injuries(sport_path):
    if sport_path not in _LEAGUE_INJ:
        data = get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/injuries") or {}
        teams = {}
        for t in data.get("injuries") or []:
            rows = []
            for i in t.get("injuries") or []:
                ath = dict(i.get("athlete") or {})
                if not ath.get("id"):
                    for ln in ath.get("links") or []:
                        m = re.search(r"/id/(\d+)", ln.get("href") or "")
                        if m:
                            ath["id"] = m.group(1)
                            break
                st = STATUS_MAP.get((i.get("status") or "").lower(), i.get("status") or "")
                rows.append({"athlete": ath, "status": st})
            teams[str(t.get("id"))] = rows
        _LEAGUE_INJ[sport_path] = teams
    return _LEAGUE_INJ[sport_path]


def load_overrides():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "injury_overrides.json")) as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    today = datetime.now(ET).strftime("%Y-%m-%d")
    return [r for r in rows if (r.get("until") or "9999") >= today]


def merge_injuries(summ, sport_path, lg, away, home):
    """Give the summary every listed player for both teams, plus manual overrides."""
    feed = league_injuries(sport_path)
    over = [r for r in load_overrides() if r.get("league") == lg]
    merged = []
    for team in (away, home):
        tid = str(team["id"])
        have = next((tm for tm in summ.get("injuries") or [] if str((tm.get("team") or {}).get("id")) == tid), None)
        rows = list((have or {}).get("injuries") or [])
        seen = {str((r.get("athlete") or {}).get("id")) for r in rows}
        for r in feed.get(tid, []):
            aid = str(r["athlete"].get("id"))
            if aid not in seen:
                rows.append(r); seen.add(aid)
        for o in over:
            if o.get("team") != team.get("abbr"):
                continue
            hit = next((r for r in rows if (r.get("athlete") or {}).get("displayName") == o["name"]), None)
            if hit:
                hit["status"] = o.get("status", "Out")
            else:
                rows.append({"athlete": {"id": o.get("id") or ("name:" + o["name"]), "displayName": o["name"],
                                         "shortName": o["name"], "position": {"abbreviation": o.get("pos", "")}},
                             "status": o.get("status", "Out")})
        merged.append({"team": {"id": team["id"]}, "injuries": rows})
    summ["injuries"] = merged
    return summ


def injury_report(summ, away, home):
    """{side: [{"id", "name", "pos", "status"}]} for questionable, doubtful and out players (ESPN injury report)."""
    out = {"away": [], "home": []}
    ids = {away["id"]: "away", home["id"]: "home"}
    for tm in summ.get("injuries", []) or []:
        side = ids.get(((tm.get("team") or {}).get("id")))
        if not side:
            continue
        for i in tm.get("injuries", []) or []:
            st = (i.get("status") or "").strip()
            if st.lower() not in ("questionable", "doubtful", "out"):
                continue
            ath = i.get("athlete") or {}
            pos = ((ath.get("position") or {}).get("abbreviation") or "").upper()
            out[side].append({"id": ath.get("id"), "name": ath.get("shortName") or ath.get("displayName"), "pos": pos, "status": st})
    return out


INJ_W = {"out": 1.0}  # only players confirmed out cost points; questionable and doubtful are shown but not counted
INJ_QB = {"nfl": 5.0, "cfb": 7.0}  # points a starting QB is worth over his backup


def injury_impact(inj, tid, per_team, key):
    """Points an injury report costs a team: {"off": points its offense loses, "def": points its defense gives up,
    "players": [(name, pos, status, points)]}.

    Only players who played in one of the team's last two games count; anyone out longer is already missing from
    the stats. The starting QB (most pass attempts in the latest game) is worth INJ_QB; backs and receivers are
    valued by their share of the team's scrimmage yards over the last 3 games; defenders by their share of tackles.
    """
    gl = per_team.get(tid, [])
    res = {"off": 0.0, "def": 0.0, "players": []}
    if not gl or not inj:
        return res
    recent = gl[-3:]
    pl = lambda g: g["teams"][tid].get("players", {})
    latest = pl(gl[-1])
    qb = max(latest.items(), key=lambda kv: kv[1].get("passAtt", 0), default=(None, {}))[0] if latest else None
    seen = set(pl(gl[-1])) | (set(pl(gl[-2])) if len(gl) > 1 else set())
    scrim = lambda q: (q.get("rushYds", 0) or 0) + (q.get("recYds", 0) or 0)
    team_scrim = sum(scrim(q) for g in recent for q in pl(g).values()) or 1.0
    team_tkl = sum((q.get("tackles", 0) or 0) for g in recent for q in pl(g).values()) or 1.0
    d_total = 0.0
    for x in inj:
        w = INJ_W.get((x.get("status") or "").lower())
        if key == "cfb" and (x.get("status") or "").lower() != "out":
            w = 0  # college injury reports are unreliable: "everyone's questionable until they play", so only Out counts
        pid = x.get("id")
        if not w or pid not in seen:
            continue
        pts, side = 0.0, "off"
        if pid == qb and latest.get(pid, {}).get("passAtt", 0) >= 10:
            pts = INJ_QB.get(key, 5.0)
        else:
            mine = [pl(g).get(pid) for g in recent if pl(g).get(pid)]
            share = sum(scrim(q) for q in mine) / team_scrim
            if share >= 0.05:
                pts = min(2.0, 6 * share)
                if (x.get("pos") or "") in ("WR", "TE"):
                    pts = min(1.0, 3 * share)  # receivers matter less than a QB or lead back: the targets go to the next man
            else:
                t_share = sum((q.get("tackles", 0) or 0) for q in mine) / team_tkl
                if t_share >= 0.04:
                    pts, side = min(0.8, 3 * t_share), "def"
        pts *= w
        if pts < 0.15:
            continue
        if side == "def":
            pts = min(pts, max(0.0, 2.5 - d_total)); d_total += pts
        res[side] += pts
        res["players"].append([x.get("name"), x.get("pos"), x.get("status"), round(pts, 1), side])
    res["off"] = round(min(res["off"], 2 * INJ_QB.get(key, 5.0)), 1)
    res["def"] = round(res["def"], 1)
    return res


BB_INJ_SCALE = 0.5  # NBA injuries count half (Nov 4): teammates take a star's shots, and the line already moves on the news
#                    (Tatum out: the formula said 6.5 points, the market moved 3)


def injury_impact_bb(inj, tid, per_team):
    """Basketball: points a team loses for injured rotation players (same shape as injury_impact).

    Each injured player is worth 0.15 x points + 0.15 x rebounds + 0.2 x assists per game in the last 5 games
    he played 15+ minutes (a 28-point star is about 6 points), scaled by the share of the team's last 5 games he
    played in (out the whole stretch = already missing from the stats), capped at 7 each and 15 per team, and
    weighted by status.
    """
    gl = per_team.get(tid, [])
    res = {"off": 0.0, "def": 0.0, "players": []}
    if not gl or not inj:
        return res
    pl = lambda g: g["teams"][tid].get("players", {})
    window = gl[-5:]
    for x in inj:
        w = INJ_W.get((x.get("status") or "").lower())
        pid = x.get("id")
        games = [pl(g)[pid] for g in window if (pl(g).get(pid) or {}).get("min", 0) >= 15]
        if not w or not games:
            continue  # out the whole window: already missing from the stats
        a = lambda k: sum(q.get(k, 0) for q in games) / len(games)
        # Count him by the share of the recent games he played in, since only those games carry his production.
        pts = min(7.0, 0.15 * a("pts") + 0.15 * a("reb") + 0.2 * a("ast")) * w * len(games) / len(window) * BB_INJ_SCALE
        if pts < 0.15:
            continue
        res["off"] += pts
        res["players"].append([x.get("name"), x.get("pos"), x.get("status"), round(pts, 1), "off"])
    res["off"] = round(min(res["off"], 15.0 * BB_INJ_SCALE), 1)
    return res


def injury_notes(g, side):
    """Warnings about the chosen team's injuries (football)."""
    inj = (g.get("inj") or {}).get(side) or []
    notes = []
    qb = [p for p in inj if p["pos"] == "QB" and p["status"].lower() == "out"]
    if qb:
        notes.append(f"{g[side]['abbr']} QB {qb[0]['name']} is {qb[0]['status'].lower()}.")
    hurt = [p for p in inj if p["pos"] != "QB" and p["pos"] in KEY_POS and p["status"].lower() == "out"]
    if len(hurt) >= 3:
        notes.append(f"{g[side]['abbr']} has {len(hurt)} starters-level players listed out ({', '.join(p['pos'] for p in hurt[:5])}).")
    return notes


# --- main ----------------------------------------------------------------------
# BACKFILL_DAY=YYYY-MM-DD (Eastern) logs model picks for college games on that day that finished before the
# pick log existed: stats come only from earlier games and prices are the closing DraftKings line.
BACKFILL = os.environ.get("BACKFILL_DAY", "").strip()
LG_TOTALS = {}  # league average combined score, filled from completed games
BB_PROP_BAR = 7.5
OVER_TAX = 0.4  # props on the over need this much more (books shade over prices)
NBA_PROP_BAR = 7.0  # basketball props need this read to be a bet (Oct backtest: 7-8 was the best band; no automatic raise)
PROP_EDGE_MIN = 0.02  # props on a Kalshi price need 2+ points of edge to be worth that price
BB_PROP_MATCHUP_W = 0.25  # basketball props (not threes): weight per point of matchup (0-10); was 0.03
BB_PROP_MPG = 30  # players under this many minutes a game need BB_PROP_BAR_LOWMIN (backtest: 30+ minute players hit best)
BB_PROP_BAR_LOWMIN = 7.5
# Prop types we bet. 3-pointer overs and rebound unders lost in the Oct backtest, so they aren't bets; points-based
# props are back in by request (Nov 4).
BB_GOOD_PROPS = {("Assists", "Over"), ("Assists", "Under"), ("Reb+Ast", "Over"), ("Reb+Ast", "Under"),
                 ("3PT Made", "Under"), ("Rebounds", "Over")} | {(c, sd) for c in ("Points", "Pts+Reb", "Pts+Ast", "PRA") for sd in ("Over", "Under")}
REB_PROP_BAR = 8.0  # basketball rebounds props: Takes went 1-4 over the first two slates
REB_BIG_LINE = 6.5  # rebound lines from here up are big men
REC_PROP_BAR = 8.0  # football receptions props (small counts, high variance)
LOW_PASS_LINE = 215.5  # QB passing unders below this carry a warning
LOW_SKILL_LINE = {"Rushing": 40.5, "Receiving": 35.5, "Rush + Rec": 50.5, "Receptions": 3.5}  # unders below these lose 0.5
NFL_PROP_FLOOR = {"Passing": 150.5, "Rushing": 30.5, "Receiving": 25.5, "Receptions": 2.5, "Rush + Rec": 35.5}  # smaller lines are backups  # basketball props: Take from here up
CBB_MIN_GAMES = 3  # college teams need this many games before the model scores their games
BB_MIN_MPG = 20  # basketball props need this many minutes a game (last 5)
KIND_OF = {"ml": "moneyline", "spread": "spread", "total": "total"}


def load_calibration(path):
    """Learn a confidence multiplier per (league, bet type) from graded model picks.

    Every logged 4+ pick counts (Takes and faded Leans), graded on the model's side at 1 unit at its logged price. A side that wins
    units (positive ROI) earns a slightly higher multiplier, one that loses units a lower one. Small
    samples barely move it (shrunk by n / (n + 30)), it needs 12 graded picks to start, and it stays within
    0.80-1.15, so one bad night can't swing the model; a pattern over weeks will.
    """
    try:
        with open(path) as f:
            picks = json.load(f).get("picks", [])
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, {}
    tally = {}
    for p in picks:
        kind = KIND_OF.get((p.get("grade") or {}).get("kind"))
        if not kind or p.get("result") not in ("W", "L") or (p.get("score") or 0) < 4:
            continue
        t = tally.setdefault((p["league"], kind), [0, 0, 0.0])
        t[0 if p["result"] == "W" else 1] += 1
        pr = p.get("price") or -110
        t[2] += (pr / 100 if pr > 0 else 100 / -pr) if p["result"] == "W" else -1
    cal = {}
    for key, (w, l, u) in tally.items():
        n = w + l
        if n < 12:
            continue
        raw = 1 + 0.8 * (u / n)  # units won per pick (ROI): at -110, +0.1 ROI is about a 57.6% hit rate
        cal[key] = round(max(0.80, min(1.15, 1 + (raw - 1) * n / (n + 30))), 3)
    return cal, {f"{k[0]}-{k[1]}": v for k, v in tally.items()}


CAL, CAL_TALLY = load_calibration(sys.argv[3]) if len(sys.argv) > 3 else ({}, {})

# The bar a Lean must clear to become a Take, per sport, set from the last three weeks of spread and total picks.
# The goal is profit: keep bets above break-even (52.4% at -110), take more Leans only when the skipped ones keep
# winning money, and never by piling on plays past a sensible daily card.
LEAN_BAR_BASE, LEAN_BAR_MIN, LEAN_BAR_MAX = 6.5, 6.0, 7.5
DAILY_CAP = {"cfb": 25, "nfl": 10, "nba": 8, "cbb": 15}  # average game plays a day before the bar won't drop
BREAKEVEN = 0.524


def lean_bars(path):
    try:
        with open(path) as f:
            picks = json.load(f).get("picks", [])
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    since = (datetime.now(ET) - timedelta(days=21)).strftime("%Y-%m-%d")
    out = {}
    u1 = lambda p: (p["price"] / 100 if p["price"] > 0 else 100 / -p["price"]) if p["result"] == "W" else -1 if p["result"] == "L" else 0
    for lg in ("cfb", "nfl", "nba", "cbb"):
        ps = [p for p in picks if p.get("league") == lg and p.get("date", "") >= since and (p.get("grade") or {}).get("kind") in ("spread", "total")]
        g = [p for p in ps if p.get("result") in ("W", "L")]
        takes = [p for p in g if p.get("verdict") in ("Take", "Take small", "Take large")]
        skipped = [p for p in g if p.get("verdict") not in ("Take", "Take small", "Take large") and 4 <= (p.get("score") or 0) < 7]
        days = len({p["date"] for p in ps}) or 1
        per_day = sum(1 for p in ps if p.get("verdict") in ("Take", "Take small", "Take large")) / days
        th = sum(p["result"] == "W" for p in takes) / len(takes) if takes else None
        sh = sum(p["result"] == "W" for p in skipped) / len(skipped) if skipped else None
        su = sum(u1(p) for p in skipped)
        tu = sum(u1(p) * (p.get("stake") or 1) for p in takes)
        bar, why = LEAN_BAR_BASE, "results are within the normal range, so the bar stays put"
        if len(takes) >= 15 and th < BREAKEVEN:
            bar = LEAN_BAR_BASE + (0.5 if th < 0.47 else 0.25)
            why = f"our bets are hitting {th:.0%}, under the {BREAKEVEN:.1%} break-even, so it's fewer, stronger plays"
        elif len(skipped) >= 15 and sh >= 0.55 and su / len(skipped) >= 0.05:
            if per_day < DAILY_CAP[lg]:
                bar = LEAN_BAR_BASE - 0.25
                why = f"skipped Leans went {sh:.0%} ({su:+.1f}u) and the card averages {per_day:.0f} plays a day, so a few more Leans make it"
            else:
                why = f"skipped Leans are winning ({su:+.1f}u), but the card already averages {per_day:.0f} plays a day, so it holds rather than add volume"
        out[lg] = {"bar": max(LEAN_BAR_MIN, min(LEAN_BAR_MAX, bar)), "why": why, "takes": [len(takes), th, round(tu, 2)],
                   "skipped": [len(skipped), sh, round(su, 2)], "perDay": round(per_day, 1)}
    return out


LEAN_BAR = lean_bars(sys.argv[3]) if len(sys.argv) > 3 else {}


def prop_tuning(path, base=NBA_PROP_BAR):
    """Every rebuild re-tunes basketball props from the last 3 weeks of graded results: the bar to bet, whether stakes
    stay at half a unit, and a small nudge for prop types (stat + over/under) that have been clearly winning or losing.
    Moves are modest and need a real sample, so one bad night can't swing the board."""
    try:
        with open(path) as f:
            picks = json.load(f).get("picks", [])
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    since = (datetime.now(ET) - timedelta(days=21)).strftime("%Y-%m-%d")
    out = {}
    for lg in ("nba", "cbb"):
        g = [p for p in picks if p.get("league") == lg and p.get("date", "") >= since and (p.get("grade") or {}).get("kind") == "prop"
             and p.get("result") in ("W", "L")]
        bets = [p for p in g if p.get("verdict") in ("Take", "Take small", "Take large")]
        u1 = lambda p: ((p["price"] / 100 if p["price"] > 0 else 100 / -p["price"]) if p["result"] == "W" else -1) * (p.get("stake") or 1)
        n, hit = len(bets), (sum(p["result"] == "W" for p in bets) / len(bets) if bets else None)
        units = round(sum(u1(p) for p in bets), 2)
        bar, why = base, "not enough graded prop bets yet to move the bar"
        if n >= 15 and hit < 0.50:
            why = f"prop bets are hitting {hit:.0%}; the bar stays at {base:g} (raising it backtested worse) and the prop-type filter does the work"
        elif n >= 40 and hit >= 0.60 and units > 0:
            bar, why = base - 0.5, f"prop bets are hitting {hit:.0%} ({units:+.1f}u), so a few more reads make it"
        elif n >= 25 and hit >= 0.57 and units > 0:
            bar, why = base - 0.25, f"prop bets are hitting {hit:.0%} ({units:+.1f}u), so the bar eases a little"
        elif n >= 15:
            why = f"prop bets are hitting {hit:.0%}, inside the normal range, so the bar holds"
        half = False  # Nov 4: props use the normal mix again (small under 8.0 or with a warning, regular from 8.0, large at 9+ when everything agrees)
        groups = {}
        tally = {}
        for p in g:
            k = f"{p['grade'].get('cat')}|{p['grade'].get('side')}"
            tally.setdefault(k, [0, 0])[p["result"] == "L"] += 1
        for k, (w, l) in tally.items():
            if w + l >= 6:
                hr = w / (w + l)
                if hr <= 0.35:
                    groups[k] = -0.5
                elif hr <= 0.42:
                    groups[k] = -0.25
                elif hr >= 0.65:
                    groups[k] = 0.3
        out[lg] = {"bar": round(bar, 2), "why": why, "half": half, "bets": [n, hit, units], "groups": groups,
                   "tally": {k: v for k, v in tally.items() if k in groups}}
    return out


PROP_TUNE = prop_tuning(sys.argv[3]) if len(sys.argv) > 3 else {}


def price_agreement(path):
    """Last 3 weeks of graded spreads and totals (4+), split by whether the price model agreed with the stat side.
    If disagreement keeps going the stats' way, the price model's weight should come down (reviewed by hand)."""
    try:
        with open(path) as f:
            saved = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    snaps, since, out = saved.get("pregame") or {}, (datetime.now(ET) - timedelta(days=21)).strftime("%Y-%m-%d"), {}
    for p in saved.get("picks", []):
        k = (p.get("grade") or {}).get("kind")
        if k not in ("spread", "total") or p.get("result") not in ("W", "L") or (p.get("score") or 0) < 4 or p.get("date", "") < since:
            continue
        pk = ((snaps.get(f"{p['league']}-{p['grade']['game']}") or {}).get("pick") or {}).get(k) or {}
        if not pk.get("valueSide"):
            continue
        rel = "agrees" if pk["valueSide"] == pk.get("side") else "disagrees" if (pk.get("value") or 0) >= 1 else None
        if rel:
            out.setdefault(p["league"], {}).setdefault(rel, [0, 0])[p["result"] == "L"] += 1
    return out


PRICE_AGREE = price_agreement(sys.argv[3]) if len(sys.argv) > 3 else {}
et_day = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")
CUR = {}
OUT_DATA = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
for lg, key in [("college-football", "cfb"), ("nfl", "nfl")]:
    cur = get(f"{BASE}/{lg}/scoreboard" + ("?groups=80&limit=400" if lg == "college-football" else ""))
    CUR[lg] = cur["week"]["number"]
    events = {}
    fbs = set()
    for w in range(1, CUR[lg] + 1):
        sb = scoreboard(lg, w) if w < CUR[lg] else cur
        for e in (sb or {}).get("events", []):
            events[e["id"]] = e
            if lg == "college-football":
                c = e["competitions"][0]
                # FBS membership: a team listed by the FBS scoreboard with a conference in group 80.
                for x in c["competitors"]:
                    fbs.add(x["team"]["id"])
    done = [eid for eid, e in events.items() if e["status"]["type"].get("completed")]
    print(lg, "weeks", CUR[lg], "events", len(events), "completed", len(done), file=sys.stderr)

    def load(eid):
        d = get(f"{BASE}/{lg}/summary?event={eid}", f"sum_{lg}_{eid}.json")
        return parse_summary(d, key) if d else None

    with ThreadPoolExecutor(16) as ex:
        games = [g for g in ex.map(load, done) if g]
    record_finals(key, games)
    LG_TOTALS[key] = sum(sum(t["pts"] for t in g["teams"].values()) for g in games) / max(1, len(games))
    if BACKFILL and key == "cfb":
        # Rebuild the board as it stood before that day's games: stats only from earlier games.
        games = [g for g in games if et_day(g["date"]) < BACKFILL]
    print(lg, "parsed", len(games), file=sys.stderr)

    # FBS universe: teams with 4+ games in the FBS scoreboard (drops FCS teams that appear only as opponents).
    counts = {}
    for g in games:
        for t in g["teams"]:
            counts[t] = counts.get(t, 0) + 1
    if lg == "college-football":
        fbs = {t for t in fbs if counts.get(t, 0) >= 4}
    else:
        fbs = set(counts)

    per_team = {t: [] for t in fbs}
    for g in sorted(games, key=lambda g: g["date"]):
        for t in g["teams"]:
            if t in per_team:
                per_team[t].append(g)

    agg = {}
    for t, gl in per_team.items():
        lines = [game_line(g, t) for g in gl]
        agg[t] = {
            "season": summarize([l[0] for l in lines]),
            "home": summarize([l[0] for l in lines if l[2] == "home" and not l[3]]),
            "away": summarize([l[0] for l in lines if l[2] == "away" and not l[3]]),
            "half": summarize([l[1] for l in lines if l[1]]) if any(l[1] for l in lines) else {},
            "last3": summarize([l[0] for l in lines[-3:]]),
        }
    ranks = {}
    sranks = {}
    for _, k, hb in OFF + DEF + SIT:
        ranks[k] = rank_map({t: a["last3"].get(k) for t, a in agg.items()}, hb)
        sranks[k] = rank_map({t: a["season"].get(k) for t, a in agg.items()}, hb)  # full-season ranks
    season_rank = {
        "off": rank_map({t: a["season"].get("pts") for t, a in agg.items()}, True),
        "def": rank_map({t: a["season"].get("o_pts") for t, a in agg.items()}, False),
    }
    mu, hfa, rat = fit_ratings(games, key, fbs if key == "cfb" else None)
    season_rank["overall"] = rank_map({t: rat[t][0] + rat[t][1] for t in fbs if t in rat}, True)
    sigma = 13.5 if key == "nfl" else 15.5

    slate_events = cur["events"]
    if key == "cfb" and not BACKFILL and slate_events and all((e.get("status") or {}).get("type", {}).get("completed") for e in slate_events):
        # Nov 8: ESPN keeps showing a finished week until midweek; move the board to next week's games (MAC Tuesday on).
        nxt = get(f"{BASE}/{lg}/scoreboard?groups=80&limit=400&week={CUR[lg] + 1}&seasontype=2") or {}
        if nxt.get("events"):
            slate_events = nxt["events"]
    slate = []
    for e in sorted(events.values() if BACKFILL and key == "cfb" else slate_events, key=lambda e: e["date"]):
        c = e["competitions"][0]
        comp = {x["homeAway"]: x for x in c["competitors"]}
        if "home" not in comp or "away" not in comp:
            continue
        summ = get(f"{BASE}/{lg}/summary?event={e['id']}")
        if not summ:
            continue
        if key == "nfl" and not BACKFILL and (e.get("status") or {}).get("type", {}).get("state") == "pre":
            ta = {"id": comp["away"]["team"]["id"], "abbr": comp["away"]["team"].get("abbreviation")}
            th = {"id": comp["home"]["team"]["id"], "abbr": comp["home"]["team"].get("abbreviation")}
            merge_injuries(summ, "football/nfl", key, ta, th)
        gi = summ.get("gameInfo", {})
        pc = (summ.get("pickcenter") or [{}])[0]
        if BACKFILL and key == "cfb":
            pc = backfill_line(key, e["id"], pc)
        venue = gi.get("venue") or c.get("venue") or {}
        addr = venue.get("address") or {}
        w = gi.get("weather") or {}
        if c.get("venue", {}).get("indoor"):
            weather = "Indoors"
        elif w:
            parts = [f"{w['temperature']}°" if w.get("temperature") is not None else None,
                     f"{w['precipitation']}% Rain" if w.get("precipitation") is not None else None,
                     f"{w['gust']}mph gusts" if w.get("gust") is not None else None]
            weather = " · ".join(p for p in parts if p) or "—"
        else:
            weather = "—"
        when = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET)
        status = e["status"]["type"]
        if BACKFILL and key == "cfb":
            if et_day(e["date"]) != BACKFILL or status.get("state") == "pre":
                continue  # rebuild started games (finished or in progress); upcoming ones keep their live board
            status = {"state": "pre"}  # score it as a pregame board (the line is ESPN's closing line)

        def team_block(x):
            t = x["team"]; tid = t["id"]
            gl = per_team.get(tid, [])
            results = []
            for g in reversed(gl):
                me = g["teams"][tid]
                op = next(v for k, v in g["teams"].items() if k != tid)
                pre = "vs " if g["neutral"] else ("@" if me["ha"] == "away" else "")
                res = "W" if me["pts"] > op["pts"] else "L" if me["pts"] < op["pts"] else "T"
                results.append([pre + op["abbr"], f"{int(me['pts'])}-{int(op['pts'])}", res])
            last5 = results[:5]
            streak = ""
            if results:
                r0 = results[0][2]; n = 0
                for r in results:
                    if r[2] != r0:
                        break
                    n += 1
                streak = f"{r0}{n}"
            rest = None
            if gl:
                # Calendar days between games in Eastern time (Sunday to Sunday = 7).
                day = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).date()
                rest = (day(e["date"]) - day(gl[-1]["date"])).days
            rank = (x.get("curatedRank") or {}).get("current")
            overall = next((r["summary"] for r in x.get("records", []) if r.get("type") == "total"), "")
            if BACKFILL:  # a rebuilt pregame board shows the record going into the game
                overall = f"{sum(r[2] == 'W' for r in results)}-{sum(r[2] == 'L' for r in results)}"
            return {"id": tid, "name": t.get("shortDisplayName") or t.get("location"), "full": t.get("displayName"),
                    "abbr": t.get("abbreviation", "?"), "rank": rank if rank and rank <= 25 else None,
                    "color": "#" + (t.get("color") or "333333"), "alt": "#" + (t.get("alternateColor") or "ffffff"),
                    "overall": overall, "streak": streak, "rest": rest, "last5": last5, "ha": x["homeAway"],
                    "loc": t.get("location"), "bet": bet_record(gl, tid)}

        away, home = team_block(comp["away"]), team_block(comp["home"])

        def stat_rows(tid, ha, spec):
            a = agg.get(tid)
            if not a:
                return [[None] * 5 for _ in spec]
            split = a["home"] if ha == "home" else a["away"]
            out = []
            for _, k, _hb in spec:
                out.append([a["season"].get(k), split.get(k), a["half"].get(k) if a["half"] else None,
                            a["last3"].get(k), ranks[k].get(tid)])
            return out

        sections = []
        for title, spec in [("Offense", OFF), ("Defense", DEF), ("Situational", SIT)]:
            A = stat_rows(away["id"], "away", spec)
            H = stat_rows(home["id"], "home", spec)
            rows = []
            rnd = lambda xs: [round(v, 2) if isinstance(v, float) else v for v in xs]
            for (name, _k, _hb), a, h in zip(spec, A, H):
                # Left: season, split, 1st half, last 3, L3 rank. Right mirrors it: L3 rank, last 3, 1st half, split, season.
                rows.append([name, rnd(a), rnd(h[::-1])])
            sections.append({"title": title, "rows": rows})
        season_ranks = {name: [sranks[k].get(away["id"]), sranks[k].get(home["id"])] for name, k, _ in OFF + DEF + SIT}

        def wl(tid):  # record going into this game, from completed games only
            r = [0, 0]
            for gg in per_team.get(tid, []):
                me = gg["teams"][tid]; op = next(v for k2, v in gg["teams"].items() if k2 != tid)
                if me["pts"] != op["pts"]:
                    r[0 if me["pts"] > op["pts"] else 1] += 1
            return r
        def letdown(tid):  # won its last game as an underdog of 7 or more
            gl = per_team.get(tid, [])
            if not gl:
                return False
            lastg = gl[-1]; me = lastg["teams"][tid]; op = next(v for k2, v in lastg["teams"].items() if k2 != tid)
            hs = (lastg.get("line") or {}).get("spread")
            if hs is None or me["pts"] <= op["pts"]:
                return False
            my_line = hs if me["ha"] == "home" else -hs
            return my_line >= 7
        ra_, rh_ = rat.get(away["id"]), rat.get(home["id"])
        power = [round(ra_[0] + ra_[1], 1), round(rh_[0] + rh_[1], 1)] if ra_ and rh_ else None
        fnet = {t: v[0] + v[1] for t, v in rat.items()}
        sos_pair = [sos_val(per_team.get(away["id"], []), away["id"], fnet), sos_val(per_team.get(home["id"], []), home["id"], fnet)]

        spread = pc.get("spread")  # home team's line
        total = pc.get("overUnder")
        aml = (pc.get("awayTeamOdds") or {}).get("moneyLine")
        hml = (pc.get("homeTeamOdds") or {}).get("moneyLine")

        # Injuries: players who have been playing and are listed out, doubtful or questionable.
        inj = injury_report(summ, away, home) if status.get("state") == "pre" else None
        inj_adj = None
        if inj:
            inj_adj = {"away": injury_impact(inj["away"], away["id"], per_team, key),
                       "home": injury_impact(inj["home"], home["id"], per_team, key)}

        # Model projection.
        model = None
        confidence = None
        ra, rh = rat.get(away["id"]), rat.get(home["id"])
        if ra and rh:
            adj = 0 if c.get("neutralSite") else hfa / 2
            pa = mu - adj + ra[0] - rh[1]
            ph = mu + adj + rh[0] - ra[1]
            if inj_adj:  # a team scores less without its injured players, and allows more without its defenders
                pa += -inj_adj["away"]["off"] + inj_adj["home"]["def"]
                ph += -inj_adj["home"]["off"] + inj_adj["away"]["def"]
            margin = ph - pa
            p_home = phi(margin / sigma)
            model = {"away": round(pa, 1), "home": round(ph, 1), "pHome": round(p_home, 3)}
            unit = 0.5 if key == "nfl" else 1.0  # points of weighted edge per confidence point
            # Edges count only as much as the model is trusted with this many games of evidence.
            w = trust(min(len(per_team.get(away["id"], [])), len(per_team.get(home["id"], []))), key)
            model["trust"] = round(w, 2)
            cs = {"moneyline": [0.0, "Pass"], "spread": [0.0, "Pass"], "total": [0.0, "Pass"]}
            if spread is not None:
                edge = w * (margin + spread)  # >0 means home covers
                s = conf(abs(edge) / unit)
                side = f"{home['abbr']} {fmt_spread(spread)}" if edge > 0 else f"{away['abbr']} {fmt_spread(-spread)}"
                cs["spread"] = [s, label(s, side)]
                model["spreadEdge"] = round(abs(edge), 1); model["spreadSide"] = side
            if total is not None:
                edge = w * ((pa + ph) - total)
                s = conf(abs(edge) / unit)
                side = ("Over " if edge > 0 else "Under ") + f"{total:g}"
                cs["total"] = [s, label(s, side)]
                model["totalEdge"] = round(abs(edge), 1); model["totalSide"] = side
            da, dh = american(aml), american(hml)
            if da and dh:
                pm = (1 / dh) / (1 / dh + 1 / da)  # market's no-vig home win probability
                pb = w * p_home + (1 - w) * pm
                ev_h, ev_a = pb * dh - 1, (1 - pb) * da - 1
                best, side, ev = (home, hml, ev_h) if ev_h >= ev_a else (away, aml, ev_a)
                s = conf(max(0, ev) * 100 / 5)
                if (side or 0) > 500:
                    s = min(s, 5.0)  # long shots: the model is least reliable here, so cap at a lean
                cs["moneyline"] = [s, label(s, f"{best['abbr']} {fmt_ml(side)}")]
                ih, ia = 1 / dh, 1 / da
                model["mlFair"] = round(ih / (ih + ia), 3)
                model["mlEV"] = round(ev, 3); model["mlSide"] = f"{best['abbr']} {fmt_ml(side)}"
            confidence = cs

        props = []
        if key == "nfl" and status.get("state") == "pre":
            injured = {(i.get("athlete") or {}).get("id") for tm in summ.get("injuries", []) for i in tm.get("injuries", [])
                       if (i.get("status") or "").lower() in OUT_STATUSES}
            lines = prop_lines(lg, e["id"])
            props = (build_props(e["id"], home["id"], away, home, per_team, agg, injured, lines)
                     + build_props(e["id"], away["id"], home, away, per_team, agg, injured, lines))
            mark_returning(props, key)
        score = None
        if status.get("state") in ("in", "post"):
            score = {"away": comp["away"].get("score"), "home": comp["home"].get("score"),
                     "detail": status.get("shortDetail") or status.get("detail")}

        # Final result for grading saved plays (scores, plus NFL player stats for props).
        final = None
        if status.get("completed"):
            fg = parse_summary(summ, key)
            if fg:
                final = {"away": num(comp["away"].get("score")), "home": num(comp["home"].get("score"))}
                if key == "nfl":
                    final["players"] = {pid: {k: v for k, v in pl.items() if k not in ("name", "full")}
                                        for t in fg["teams"].values() for pid, pl in t.get("players", {}).items()}

        def rk(table, tid):
            return table.get(tid)

        slate.append({
            "id": e["id"], "iso": e["date"], "away": away, "home": home,
            "date": when.strftime("%b %-d · %-I:%M %p ET"),
            "location": ", ".join(v for v in [addr.get("city"), addr.get("state")] if v) or venue.get("fullName", "—"),
            "stadium": venue.get("fullName"), "neutral": c.get("neutralSite", False),
            "weather": weather, "surface": ("Grass" if venue.get("grass") else "Turf") if "grass" in venue else "—",
            "offRank": [rk(season_rank["off"], away["id"]), rk(season_rank["off"], home["id"])],
            "defRank": [rk(season_rank["def"], away["id"]), rk(season_rank["def"], home["id"])],
            "overallRank": [rk(season_rank["overall"], away["id"]), rk(season_rank["overall"], home["id"])],
            "lines": {"book": (pc.get("provider") or {}).get("name"),
                      "awaySpread": fmt_spread(-spread) if spread is not None else "—", "awayML": fmt_ml(aml),
                      "total": f"{total:g}" if total is not None else "—", "homeML": fmt_ml(hml),
                      "homeSpread": fmt_spread(spread) if spread is not None else "—",
                      "awaySpreadOdds": fmt_ml((pc.get("awayTeamOdds") or {}).get("spreadOdds")),
                      "homeSpreadOdds": fmt_ml((pc.get("homeTeamOdds") or {}).get("spreadOdds")),
                      "overOdds": fmt_ml(pc.get("overOdds")), "underOdds": fmt_ml(pc.get("underOdds")),
                      "spreadNum": spread, "totalNum": total},
            "sections": sections, "confidence": confidence, "model": model,
            "state": status.get("state"), "score": score,
            "fbs": [away["id"] in agg, home["id"] in agg], "props": props, "final": final, "inj": inj,
            "seasonRanks": season_ranks, "power": power, "sosVal": sos_pair, "wl": [wl(away["id"]), wl(home["id"])], "injAdj": inj_adj,
            "letdown": {"away": letdown(away["id"]), "home": letdown(home["id"])},
        })
    enrich(key, slate)
    OUT_DATA[key] = slate
    OUT_DATA[key + "_meta"] = {"week": CUR[lg], "teams": len(agg), "games": len(games)}
    print(lg, "slate", len(slate), file=sys.stderr)

# --- basketball (NBA + men's college) -------------------------------------------
BB = "https://site.api.espn.com/apis/site/v2/sports/basketball"
BB_PROPS = [  # label, value from a player game, markets, opponent-allowed keys, min season avg
    ("Points", lambda p: p["pts"], "Total Points", ["pts"], 8),
    ("Rebounds", lambda p: p["reb"], "Total Rebounds", ["reb"], 3),
    ("Assists", lambda p: p["ast"], "Total Assists", ["ast"], 2),
    ("PRA", lambda p: p["pts"] + p["reb"] + p["ast"], "Total Points, Rebounds, and Assists", ["pts", "reb", "ast"], 12),
    ("Pts+Reb", lambda p: p["pts"] + p["reb"], "Total Points and Rebounds", ["pts", "reb"], 10),
    ("Pts+Ast", lambda p: p["pts"] + p["ast"], "Total Points and Assists", ["pts", "ast"], 10),
    ("Reb+Ast", lambda p: p["reb"] + p["ast"], "Total Assists and Rebounds", ["reb", "ast"], 5),
    ("3PT Made", lambda p: p["tpm"], "Total 3-Point Field Goals", ["tpm"], 0.8),
    # Steals + blocks left off: not something we bet.
]
BB_COUNT = {"3PT Made", "Blk+Stl"}
TEAM_OUT = 0.15  # teammates out who made this share of the team's season production in the stat: unders are off
BACK_MIN_MPG = 24  # a regular (this many minutes) who missed the last game and isn't on the injury report is back
BACK_SHARE = 0.12  # ...and if he makes this share of the stat, teammates' overs get a warning
BB_VAC_MIN = 0.10  # share of the team's recent production (this stat) belonging to teammates now out
BB_FLOOR = {"Points": 4, "Rebounds": 2, "Assists": 1.5, "PRA": 5, "Pts+Reb": 4.5, "Pts+Ast": 4.5, "Reb+Ast": 2.5}


def mk(v):
    a, _, b = (v or "0-0").partition("-")
    return num(a), num(b)


def parse_bb(d):
    comp = d["header"]["competitions"][0]
    teams = {}
    for c in comp["competitors"]:
        teams[c["team"]["id"]] = {"id": c["team"]["id"], "abbr": c["team"].get("abbreviation", "?"), "ha": c["homeAway"],
                                  "pts": num(c.get("score"))}
    if len(teams) != 2:
        return None
    for t in d.get("boxscore", {}).get("teams", []):
        r = teams.get(t["team"]["id"])
        if r is None:
            continue
        st = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        r["fgm"], r["fga"] = mk(st.get("fieldGoalsMade-fieldGoalsAttempted"))
        r["tpm"], r["tpa"] = mk(st.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
        r["ftm"], r["fta"] = mk(st.get("freeThrowsMade-freeThrowsAttempted"))
        for k, src in [("reb", "totalRebounds"), ("oreb", "offensiveRebounds"), ("ast", "assists"), ("stl", "steals"),
                       ("blk", "blocks"), ("tov", "turnovers"), ("fb", "fastBreakPoints"), ("paint", "pointsInPaint")]:
            r[k] = num(st.get(src) if st.get(src) is not None else st.get("turnovers") if k == "tov" else None)
        r["poss"] = r["fga"] - r["oreb"] + r["tov"] + 0.44 * r["fta"]
    for t in d.get("boxscore", {}).get("players", []):
        r = teams.get(t["team"]["id"])
        if r is None:
            continue
        for cat in t.get("statistics", [])[:1]:
            labels = cat.get("labels") or []
            for a in cat.get("athletes", []):
                v = dict(zip(labels, a.get("stats", [])))
                ath = a.get("athlete") or {}
                if not ath.get("id") or not v or num(v.get("MIN")) <= 0:
                    continue
                r.setdefault("players", {})[ath["id"]] = {
                    "name": ath.get("shortName") or ath.get("displayName"), "full": ath.get("displayName"),
                    "min": num(v.get("MIN")), "pts": num(v.get("PTS")), "reb": num(v.get("REB")), "ast": num(v.get("AST")),
                    "stl": num(v.get("STL")), "blk": num(v.get("BLK")), "tpm": mk(v.get("3PT"))[0], "tpa": mk(v.get("3PT"))[1],
                    "fgm": mk(v.get("FG"))[0], "fga": mk(v.get("FG"))[1], "fta": mk(v.get("FT"))[1],
                    "oreb": num(v.get("OREB")), "dreb": num(v.get("DREB")), "tov": num(v.get("TO")),
                    "pos": ((ath.get("position") or {}).get("abbreviation") or "")}
    if any("fga" not in r for r in teams.values()):
        return None
    return {"id": comp["id"], "date": comp["date"], "neutral": comp.get("neutralSite", False), "teams": teams, "line": game_lines(d)}


BB_ROWS = [  # (section, label, key, higher_is_better, kind)
    ("Offense", "PPG", "pts", True, "n"), ("Offense", "2PT%", "p2", True, "pct"), ("Offense", "3PT%", "p3", True, "pct"),
    ("Offense", "FT%", "pft", True, "pct"), ("Offense", "Paint PPG", "paint", True, "n"),
    ("Defense", "Opp PPG", "o_pts", False, "n"), ("Defense", "Opp 2PT%", "o_p2", False, "pct"),
    ("Defense", "Opp 3PT%", "o_p3", False, "pct"), ("Defense", "Opp FT%", "o_pft", False, "pct"),
    ("Defense", "Opp Paint PPG", "o_paint", False, "n"),
    ("Supporting Statistics", "Rebounds/G", "reb", True, "n"), ("Supporting Statistics", "Assists/G", "ast", True, "n"),
    ("Supporting Statistics", "BLK+STL/G", "stlblk", True, "n"), ("Supporting Statistics", "Turnovers/G", "tov", False, "n"),
    ("Supporting Statistics", "Fast Break", "fb", True, "n"), ("Supporting Statistics", "Team Pace", "pace", True, "n"),
]


def bb_window(gl, tid, per_game_pct=False):
    """Averages over a list of games from team tid's perspective. Shooting percentages pool makes and attempts,
    or (per_game_pct, used for the last-5 window as on the Profit Hunt boards) average each game's percentage."""
    if not gl:
        return {}
    S = lambda f: sum(f(g) for g in gl)
    me = lambda g: g["teams"][tid]
    op = lambda g: next(v for k, v in g["teams"].items() if k != tid)
    n = len(gl)
    if per_game_pct:
        def pct(fm, fa):  # fm, fa: per-game functions
            vals = [100 * fm(g) / fa(g) for g in gl if fa(g)]
            return sum(vals) / len(vals) if vals else None
        P = lambda fm, fa: pct(fm, fa)
    else:
        P = lambda fm, fa: (100 * S(fm) / S(fa)) if S(fa) else None
    return {
        "pts": S(lambda g: me(g)["pts"]) / n, "paint": S(lambda g: me(g)["paint"]) / n,
        "p2": P(lambda g: me(g)["fgm"] - me(g)["tpm"], lambda g: me(g)["fga"] - me(g)["tpa"]),
        "p3": P(lambda g: me(g)["tpm"], lambda g: me(g)["tpa"]), "pft": P(lambda g: me(g)["ftm"], lambda g: me(g)["fta"]),
        "o_pts": S(lambda g: op(g)["pts"]) / n, "o_paint": S(lambda g: op(g)["paint"]) / n,
        "o_p2": P(lambda g: op(g)["fgm"] - op(g)["tpm"], lambda g: op(g)["fga"] - op(g)["tpa"]),
        "o_p3": P(lambda g: op(g)["tpm"], lambda g: op(g)["tpa"]), "o_pft": P(lambda g: op(g)["ftm"], lambda g: op(g)["fta"]),
        "reb": S(lambda g: me(g)["reb"]) / n, "ast": S(lambda g: me(g)["ast"]) / n,
        "stlblk": S(lambda g: me(g)["stl"] + me(g)["blk"]) / n, "tov": S(lambda g: me(g)["tov"]) / n,
        "fb": S(lambda g: me(g)["fb"]) / n, "pace": S(lambda g: (me(g)["poss"] + op(g)["poss"]) / 2) / n,
        "o_reb": S(lambda g: op(g)["reb"]) / n, "o_ast": S(lambda g: op(g)["ast"]) / n, "o_tpm": S(lambda g: op(g)["tpm"]) / n,
        "o_stl": S(lambda g: op(g)["stl"]) / n, "o_blk": S(lambda g: op(g)["blk"]) / n,
        "ortg": 100 * S(lambda g: me(g)["pts"]) / max(1, S(lambda g: me(g)["poss"])),
        "drtg": 100 * S(lambda g: op(g)["pts"]) / max(1, S(lambda g: op(g)["poss"])),
    }


TORVIK_ALIAS = {"2026": "Appalachian St.", "2856": "Cal Baptist", "2755": "Grambling St.", "140": "UMKC", "2443": "New Orleans",
                "112358": "LIU", "2377": "McNeese St.", "2390": "Miami FL", "2447": "Nicholls St.", "145": "Mississippi",
                "2437": "Nebraska Omaha", "2545": "Southeastern Louisiana", "2534": "Sam Houston St.", "23": "San Jose St.",
                "2547": "Seattle", "2908": "USC Upstate", "357": "Texas A&M Corpus Chris", "399": "Albany", "41": "Connecticut",
                "82": "Illinois Chicago", "2433": "Louisiana Monroe", "2630": "Tennessee Martin"}  # ESPN team id -> Torvik name
TORVIK_HCA = 3.0  # home-court points on top of Torvik's neutral-court efficiencies


def _tnorm(s_):
    s_ = (s_ or "").lower().replace("&", "and").replace("'", "").replace(".", "")
    s_ = re.sub(r"\bstate\b", "st", s_)
    s_ = re.sub(r"\bsaint\b", "st", s_)
    return re.sub(r"[^a-z0-9]", "", s_)


def torvik_ratings():
    """Bart Torvik's public T-Rank team file (adjusted offense/defense per 100 possessions and tempo). A tool for the
    college price model; our own confidence math stays in charge. None if the file can't be read."""
    now = datetime.now(ET)
    season = now.year + 1 if now.month >= 8 else now.year
    try:
        req = urllib.request.Request(f"https://barttorvik.com/{season}_team_results.csv", headers={"User-Agent": "gushers-picks/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
        import csv, io
        rows = [x for x in csv.DictReader(io.StringIO(text)) if x.get("adjoe") and x.get("adjde") and x.get("adjt")]
    except Exception as e:  # network or format change: run without it
        print("torvik unavailable:", type(e).__name__, getattr(e, "code", ""), file=sys.stderr)
        return None
    if len(rows) < 300:
        return None
    by = {_tnorm(x["team"]): {"oe": float(x["adjoe"]), "de": float(x["adjde"]), "t": float(x["adjt"]), "name": x["team"]} for x in rows}
    oe = sum(v["oe"] for v in by.values()) / len(by)
    te = sum(v["t"] for v in by.values()) / len(by)
    return {"by": by, "oe": oe, "te": te}


def torvik_game(tv, away, home, neutral):
    """Torvik projection for one game: points each side, margin (home) and total."""
    if not tv:
        return None
    def find(t):
        if str(t.get("id")) in TORVIK_ALIAS:
            return tv["by"].get(_tnorm(TORVIK_ALIAS[str(t["id"])]))
        for c in (t.get("loc"), t.get("name")):
            if c and _tnorm(c) in tv["by"]:
                return tv["by"][_tnorm(c)]
        return None
    a, h = find(away), find(home)
    if not a or not h:
        return None
    poss = a["t"] * h["t"] / tv["te"]
    hca = 0.0 if neutral else TORVIK_HCA
    ph = h["oe"] * a["de"] / tv["oe"] * poss / 100 + hca / 2
    pa = a["oe"] * h["de"] / tv["oe"] * poss / 100 - hca / 2
    return {"away": round(pa, 1), "home": round(ph, 1), "margin": round(ph - pa, 1), "total": round(pa + ph, 1)}


def rest_spot(gl, when, tid):
    """Played yesterday (ET)? None if rested, else 'road' or 'home' for where that game was (shown on the card only:
    Nov 9 backtest, tired teams went 15-9 against the spread, so the market already prices it)."""
    y = (when.astimezone(ET).date() - timedelta(days=1))
    for g_ in gl[::-1]:
        try:
            d_ = datetime.fromisoformat(g_["date"].replace("Z", "+00:00")).astimezone(ET).date()
        except Exception:
            continue
        if d_ == y:
            return "road" if (g_["teams"].get(tid) or {}).get("ha") == "away" else "home"
        if d_ < y:
            break
    return None


def run_basketball(lg, key, start, groups):
    today = datetime.now(ET).date()
    q = f"&groups={groups}&limit=400" if groups else ""
    events = {}
    day = start
    while day <= today:
        ds = day.strftime("%Y%m%d")
        sb = get(f"{BB}/{lg}/scoreboard?dates={ds}{q}", f"bsb_{lg}_{ds}.json" if day < today - timedelta(days=1) else None)
        for e in (sb or {}).get("events", []):
            if (e.get("season") or {}).get("type") in (2, 3) or "season" not in e:
                events[e["id"]] = e
        day += timedelta(days=1)
    done = [eid for eid, e in events.items() if e["status"]["type"].get("completed")]

    def load(eid):
        d = get(f"{BB}/{lg}/summary?event={eid}", f"bsum_{lg}_{eid}.json")
        return parse_bb(d) if d else None

    with ThreadPoolExecutor(24) as ex:
        games = sorted([g for g in ex.map(load, done) if g], key=lambda g: g["date"])
    record_finals(key, games)
    if BACKFILL and key == "nba":
        games = [g for g in games if et_day(g["date"]) < BACKFILL]  # pregame view of that day: earlier games only
    print(lg, "completed", len(games), file=sys.stderr)
    if key == "cbb":
        # Nov 9: games against non-Division I schools (40-point wins over NAIA teams) inflated early ratings and stats,
        # so college numbers use Division I opponents only (closer to the market: 0.49 -> 0.58 correlation with lines).
        d1_ = get(f"{BB}/{lg}/teams?groups=50&limit=500", "cbb_d1_teams.json") or {}
        d1_set = {t["team"]["id"] for sp in d1_.get("sports", []) for l_ in sp.get("leagues", []) for t in l_.get("teams", [])}
        if len(d1_set) > 300:
            games = [g for g in games if all(t in d1_set for t in g["teams"])]
            print(lg, "D1-vs-D1 games", len(games), file=sys.stderr)
    counts = {}
    for g in games:
        for t in g["teams"]:
            counts[t] = counts.get(t, 0) + 1
    # Stats show after a team's first game; college picks wait for 3 games each (see CBB_MIN_GAMES below).
    teams_in = {t for t, c in counts.items() if c >= 1}
    per_team = {t: [g for g in games if t in g["teams"]] for t in teams_in}
    agg = {t: {"season": bb_window(gl, t), "home": bb_window([g for g in gl if g["teams"][t]["ha"] == "home" and not g["neutral"]], t),
               "away": bb_window([g for g in gl if g["teams"][t]["ha"] == "away" and not g["neutral"]], t),
               "l10": bb_window(gl[-10:], t), "l5": bb_window(gl[-5:], t, per_game_pct=True)} for t, gl in per_team.items() if gl}
    if key == "cbb":
        # Ranks are among Division I teams only (about 362), so a D-II/NAIA opponent's stats don't push the
        # numbers past 400. Those teams keep their stats; they just aren't ranked.
        d1 = get(f"{BB}/{lg}/teams?groups=50&limit=500", "cbb_d1_teams.json") or {}
        d1_ids = {t["team"]["id"] for sp in d1.get("sports", []) for l_ in sp.get("leagues", []) for t in l_.get("teams", [])}
        rk_agg = {t: a for t, a in agg.items() if t in d1_ids} if len(d1_ids) > 300 else agg
    else:
        rk_agg = agg
    ranks = {k: rank_map({t: a["l5"].get(k) for t, a in rk_agg.items()}, hb) for _, _, k, hb, _ in BB_ROWS}
    nteams = len(rk_agg)
    mu, hfa, rat = fit_ratings(games, key, teams_in) if games else (0, 0, {})
    net = {t: rat[t][0] + rat[t][1] for t in agg if t in rat}
    srank = {"off": rank_map({t: a["season"].get("ortg") for t, a in rk_agg.items()}, True),
             "def": rank_map({t: a["season"].get("drtg") for t, a in rk_agg.items()}, False),
             "overall": rank_map({t: v for t, v in net.items() if t in rk_agg}, True),
             # strength of schedule over the last 5: average opponent rating, 1st = toughest
             "sos": rank_map({t: sum(net.get(next(k for k in g["teams"] if k != t), 0) for g in gl[-5:]) / max(1, len(gl[-5:]))
                              for t, gl in per_team.items() if gl and t in rk_agg}, True)}
    sigma, unit = (12.0, 0.45) if key == "nba" else (11.0, 0.6)
    tvk = None  # Torvik blocks automated downloads from GitHub's servers (403, Nov 9), so it's off; see torvik_ratings

    cur = get(f"{BB}/{lg}/scoreboard?{q.lstrip('&')}")
    if not (BACKFILL and key == "nba"):
        # ESPN's default basketball day lags into the morning (on Nov 2 it still showed Sunday's games at 10 AM),
        # so ask for today's ET date: every game on today's slate goes on the board, scored or not.
        today = None
        for ahead in range(4):  # an off day (Nov 3, Election Day) shows the next slate instead of yesterday's games
            day_ = (datetime.now(ET) + timedelta(days=ahead)).strftime('%Y%m%d')
            today = get(f"{BB}/{lg}/scoreboard?dates={day_}&{q.lstrip('&')}")
            if (today or {}).get("events"):
                break
        if (today or {}).get("events"):
            seen = {e["id"] for e in today["events"]}
            today["events"] += [e for e in (cur or {}).get("events", []) if e["id"] not in seen
                                and ((e.get("status") or {}).get("type") or {}).get("state") != "post"]
            cur = today
            if key in ("cbb", "nba"):
                # Basketball (Nov 8): the next day's slate goes on the board too, so its picks are out the night before.
                nxt_ = (datetime.strptime(day_, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
                nx = get(f"{BB}/{lg}/scoreboard?dates={nxt_}&{q.lstrip('&')}") or {}
                seen = {e["id"] for e in cur["events"]}
                cur["events"] += [e for e in nx.get("events", []) if e["id"] not in seen]
    evs = (cur or {}).get("events", [])
    with ThreadPoolExecutor(16) as ex:  # fetch summaries in parallel: big college slates have 150+ games
        summaries = dict(zip([e["id"] for e in evs], ex.map(lambda e: get(f"{BB}/{lg}/summary?event={e['id']}"), evs)))
    slate = []
    for e in sorted((cur or {}).get("events", []), key=lambda e: e["date"]):
        c = e["competitions"][0]
        comp = {x["homeAway"]: x for x in c["competitors"]}
        if "home" not in comp or "away" not in comp or (e.get("season") or {}).get("type", 2) not in (2, 3):
            continue  # skip exhibitions and preseason
        summ = summaries.get(e["id"])
        if not summ:
            continue
        if key == "nba" and not BACKFILL and (e.get("status") or {}).get("type", {}).get("state") == "pre":
            ta = {"id": comp["away"]["team"]["id"], "abbr": comp["away"]["team"].get("abbreviation")}
            th = {"id": comp["home"]["team"]["id"], "abbr": comp["home"]["team"].get("abbreviation")}
            merge_injuries(summ, "basketball/nba", key, ta, th)
        pc = (summ.get("pickcenter") or [{}])[0]
        venue = (summ.get("gameInfo") or {}).get("venue") or c.get("venue") or {}
        addr = venue.get("address") or {}
        when = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET)
        status = e["status"]["type"]
        if BACKFILL and key == "nba":
            if et_day(e["date"]) != BACKFILL or status.get("state") == "pre":
                continue
            status = {"state": "pre"}

        def team_block(x):
            t = x["team"]; tid = t["id"]
            gl = per_team.get(tid, [])
            res = []
            for g in reversed(gl):
                me, op = g["teams"][tid], next(v for k, v in g["teams"].items() if k != tid)
                res.append([("vs " if g["neutral"] else "@" if me["ha"] == "away" else "") + op["abbr"],
                            f"{int(me['pts'])}-{int(op['pts'])}", "W" if me["pts"] > op["pts"] else "L"])
            streak = ""
            if res:
                n = 0
                for r in res:
                    if r[2] != res[0][2]:
                        break
                    n += 1
                streak = f"{res[0][2]}{n}"
            dday = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).date()
            rank = (x.get("curatedRank") or {}).get("current")
            return {"id": tid, "name": t.get("shortDisplayName") or t.get("location"), "full": t.get("displayName"),
                    "abbr": t.get("abbreviation", "?"), "rank": rank if rank and rank <= 25 else None,
                    "color": "#" + (t.get("color") or "333333"), "alt": "#" + (t.get("alternateColor") or "ffffff"),
                    "overall": (f"{sum(r[2] == 'W' for r in res)}-{sum(r[2] == 'L' for r in res)}" if BACKFILL else
                                next((r["summary"] for r in x.get("records", []) if r.get("type") == "total"), "")),
                    "streak": streak, "rest": (dday(e["date"]) - dday(gl[-1]["date"])).days if gl else None,
                    "last5": res[:5], "ha": x["homeAway"], "loc": t.get("location"), "bet": bet_record(gl, tid)}

        away, home = team_block(comp["away"]), team_block(comp["home"])
        rnd = lambda v: round(v, 2) if isinstance(v, float) else v
        sections = []
        for title in ["Offense", "Defense", "Supporting Statistics"]:
            rows = []
            for sec, lab, k, _hb, kind in BB_ROWS:
                if sec != title:
                    continue
                a, h = agg.get(away["id"], {}), agg.get(home["id"], {})
                L = [rnd((a.get(w) or {}).get(k)) for w in ("season", "away", "l10", "l5")] + [ranks[k].get(away["id"])]
                R = [rnd((h.get(w) or {}).get(k)) for w in ("season", "home", "l10", "l5")] + [ranks[k].get(home["id"])]
                rows.append([lab, L, R[::-1], kind])
            sections.append({"title": title, "rows": rows, "single": title.startswith("Supporting")})

        spread, total = pc.get("spread"), pc.get("overUnder")
        aml, hml = (pc.get("awayTeamOdds") or {}).get("moneyLine"), (pc.get("homeTeamOdds") or {}).get("moneyLine")
        inj = injury_report(summ, away, home) if status.get("state") == "pre" else None
        inj_adj = ({"away": injury_impact_bb(inj["away"], away["id"], per_team), "home": injury_impact_bb(inj["home"], home["id"], per_team)}
                   if inj and key == "nba" else None)
        model, confidence = None, None
        ra, rh = rat.get(away["id"]), rat.get(home["id"])
        gp_min = min(counts.get(away["id"], 0), counts.get(home["id"], 0))
        # College (Nov 8): a confidence read as soon as both teams have played; plays wait for CBB_MIN_GAMES each.
        enough = key != "cbb" or gp_min >= 1
        early = key == "cbb" and gp_min < CBB_MIN_GAMES
        tvp = torvik_game(tvk, away, home, c.get("neutralSite")) if tvk else None
        if ra and rh and enough:
            adj = 0 if c.get("neutralSite") else hfa / 2
            pa, ph = mu - adj + ra[0] - rh[1], mu + adj + rh[0] - ra[1]
            if inj_adj:  # missing rotation players cost their team points
                pa -= inj_adj["away"]["off"]; ph -= inj_adj["home"]["off"]
            margin, p_home = ph - pa, phi((ph - pa) / sigma)
            model = {"away": round(pa, 1), "home": round(ph, 1), "pHome": round(p_home, 3)}
            w = trust(min(len(per_team.get(away["id"], [])), len(per_team.get(home["id"], []))), key)
            model["trust"] = round(w, 2)
            cs = {"moneyline": [0.0, "Pass"], "spread": [0.0, "Pass"], "total": [0.0, "Pass"]}
            if spread is not None:
                edge = w * (margin + spread)
                sc = conf(abs(edge) / unit)
                side = f"{home['abbr']} {fmt_spread(spread)}" if edge > 0 else f"{away['abbr']} {fmt_spread(-spread)}"
                cs["spread"] = [sc, label(sc, side)]; model.update(spreadEdge=round(abs(edge), 1), spreadSide=side)
            if total is not None:
                edge = w * (pa + ph - total)
                sc = conf(abs(edge) / (unit * 1.5))
                side = ("Over " if edge > 0 else "Under ") + f"{total:g}"
                cs["total"] = [sc, label(sc, side)]; model.update(totalEdge=round(abs(edge), 1), totalSide=side)
            da, dh = american(aml), american(hml)
            if da and dh:
                pm = (1 / dh) / (1 / dh + 1 / da)
                pb = w * p_home + (1 - w) * pm
                ev_h, ev_a = pb * dh - 1, (1 - pb) * da - 1
                best, ml, ev = (home, hml, ev_h) if ev_h >= ev_a else (away, aml, ev_a)
                sc = conf(max(0, ev) * 100 / 5)
                if (ml or 0) > 500:
                    sc = min(sc, 5.0)
                cs["moneyline"] = [sc, label(sc, f"{best['abbr']} {fmt_ml(ml)}")]
                model.update(mlEV=round(ev, 3), mlSide=f"{best['abbr']} {fmt_ml(ml)}")
            confidence = cs

        props = []
        if key == "nba" and status.get("state") == "pre":
            injured = {(i.get("athlete") or {}).get("id") for tm in summ.get("injuries", []) for i in tm.get("injuries", [])
                       if (i.get("status") or "").lower() in OUT_STATUSES | {"out for season"}}
            props = bb_props(e["id"], away, home, per_team, agg, injured, nteams)
        score = None
        if status.get("state") in ("in", "post"):
            score = {"away": comp["away"].get("score"), "home": comp["home"].get("score"),
                     "detail": status.get("shortDetail") or status.get("detail")}
        final = None
        if status.get("completed"):
            fg = parse_bb(summ)
            if fg:
                final = {"away": num(comp["away"].get("score")), "home": num(comp["home"].get("score")),
                         "players": {pid: {k: v for k, v in pl.items() if k not in ("name", "full")}
                                     for t in fg["teams"].values() for pid, pl in t.get("players", {}).items()} if key == "nba" else {}}
        ab, hb_ = away["abbr"], home["abbr"]
        r = lambda tbl, tid: tbl.get(tid)
        if key == "cbb" and model is None and tvp:
            # A team with no games yet: Torvik's preseason-based ratings carry the read (never a play while early).
            model = {"away": tvp["away"], "home": tvp["home"], "pHome": round(phi(tvp["margin"] / sigma), 3), "trust": 0.6, "src": "torvik"}
            confidence = {"moneyline": [0.0, "Pass"], "spread": [0.0, "Pass"], "total": [0.0, "Pass"]}
        slate.append({
            "id": e["id"], "iso": e["date"], "away": away, "home": home, "date": when.strftime("%b %-d · %-I:%M %p ET"),
            "location": ", ".join(v for v in [addr.get("city"), addr.get("state")] if v) or venue.get("fullName", "—"),
            "neutral": c.get("neutralSite", False), "early": gp_min if early else None, "torvik": tvp,
            "b2b": {sd_: rest_spot(per_team.get(t_["id"], []), when, t_["id"]) for sd_, t_ in (("away", away), ("home", home))},
            "strip": [["Date / Time", when.strftime("%b %-d · %-I:%M %p ET")],
                      ["Location", ", ".join(v for v in [addr.get("city"), addr.get("state")] if v) or "—"],
                      ["L5 SOS", [r(srank["sos"], away["id"]), r(srank["sos"], home["id"])]],
                      ["Off Rank", [r(srank["off"], away["id"]), r(srank["off"], home["id"])]],
                      ["Def Rank", [r(srank["def"], away["id"]), r(srank["def"], home["id"])]],
                      ["Overall Rank", [r(srank["overall"], away["id"]), r(srank["overall"], home["id"])]]],
            "cols": ["Season", "Last 10", "Last 5", "L5 Rank"],
            "lines": {"book": (pc.get("provider") or {}).get("name"),
                      "awaySpread": fmt_spread(-spread) if spread is not None else "—", "awayML": fmt_ml(aml),
                      "total": f"{total:g}" if total is not None else "—", "homeML": fmt_ml(hml),
                      "homeSpread": fmt_spread(spread) if spread is not None else "—",
                      "awaySpreadOdds": fmt_ml((pc.get("awayTeamOdds") or {}).get("spreadOdds")),
                      "homeSpreadOdds": fmt_ml((pc.get("homeTeamOdds") or {}).get("spreadOdds")),
                      "overOdds": fmt_ml(pc.get("overOdds")), "underOdds": fmt_ml(pc.get("underOdds")),
                      "spreadNum": spread, "totalNum": total},
            "sections": sections, "confidence": confidence, "model": model, "state": status.get("state"),
            "score": score, "final": final, "props": props, "fbs": [True, True], "injAdj": inj_adj, "inj": inj,
            "sosVal": [sos_val(per_team.get(away["id"], []), away["id"], net), sos_val(per_team.get(home["id"], []), home["id"], net)],
        })
    enrich(key, slate)
    OUT_DATA[key] = slate
    OUT_DATA[key + "_meta"] = {"teams": nteams, "games": len(games), "sport": "bb"}
    print(lg, "slate", len(slate), file=sys.stderr)


def bb_props(eid, away, home, per_team, agg, injured, nteams):
    lines = {}
    odds = get(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds?lang=en&region=us")
    items = (odds or {}).get("items") or []
    if items and "propBets" in items[0]:
        url = items[0]["propBets"]["$ref"].replace("http://", "https://")
        markets = {m for _, _, m, _, _ in BB_PROPS}
        page = 1
        while True:
            d = get(f"{url}&limit=1000&page={page}")
            for it in (d or {}).get("items", []):
                name = (it.get("type") or {}).get("name")
                aid = ((it.get("athlete") or {}).get("$ref") or "").split("/athletes/")[-1].split("?")[0]
                tgt = ((it.get("current") or {}).get("target") or {}).get("value")
                if name in markets and aid and tgt is not None:
                    lines[(name, aid)] = (tgt, ((it.get("open") or {}).get("target") or {}).get("value"))
            if not d or page >= d.get("pageCount", 1):
                break
            page += 1
    rows = []
    for team, opp in [(away, home), (home, away)]:
        tid = team["id"]
        gl = per_team.get(tid, [])
        if not gl:
            continue
        latest = {}
        for gg in gl[-3:]:  # anyone from the last 3 games, so a player back from a missed game still gets lines
            latest.update(gg["teams"][tid].get("players", {}))
        # Teammates newly out (they played in the last 3 games): their points, boards and assists go to whoever plays.
        team_tot, gone_tot, gone_names = {}, {}, {}
        for gg in gl[-3:]:
            for q_id, q in (gg["teams"][tid].get("players") or {}).items():
                for k in ("pts", "reb", "ast", "tpm"):
                    team_tot[k] = team_tot.get(k, 0) + (q.get(k) or 0)
                    if q_id in injured:
                        gone_tot[k] = gone_tot.get(k, 0) + (q.get(k) or 0)
                        gone_names[q_id] = q.get("name")
        # Season view: teammates out (any length of time) and regulars coming back (missed the last game, not on the
        # injury report). Nov 2: unders lost when teammates were out (SGA, Dosunmu, Clingan); Maxey's three over lost
        # with Embiid back.
        s_tot, s_gone, s_ply, names_all, mins_all = {}, {}, {}, {}, {}
        last_ids = set((gl[-1]["teams"][tid].get("players") or {}).keys())
        for gg in gl:
            for q_id, q in (gg["teams"][tid].get("players") or {}).items():
                names_all[q_id] = q.get("name")
                mins_all.setdefault(q_id, []).append(q.get("min") or 0)
                for k in ("pts", "reb", "ast", "tpm"):
                    s_tot[k] = s_tot.get(k, 0) + (q.get(k) or 0)
                    s_ply.setdefault(q_id, {})[k] = s_ply.get(q_id, {}).get(k, 0) + (q.get(k) or 0)
                    if q_id in injured:
                        s_gone[k] = s_gone.get(k, 0) + (q.get(k) or 0)
        out_names = sorted({names_all[i] for i in names_all if i in injured and s_ply.get(i, {}).get("pts", 0) > 0},
                           key=lambda n: -max((s_ply[i].get("pts", 0) for i in names_all if names_all[i] == n), default=0))
        back_ids = [i for i, m in mins_all.items() if len(gl) >= 3 and i not in last_ids and i not in injured
                    and len(m) >= 2 and sum(m) / len(m) >= BACK_MIN_MPG]
        for label, val, market, okeys, min_avg in BB_PROPS:
            tt = sum(team_tot.get(k, 0) for k in okeys)
            vac_share = (sum(gone_tot.get(k, 0) for k in okeys) / tt) if tt and label != "Blk+Stl" else 0.0
            st_ = sum(s_tot.get(k, 0) for k in okeys)
            out_share = (sum(s_gone.get(k, 0) for k in okeys) / st_) if st_ else 0.0
            back = [(i, names_all[i], sum(s_ply[i].get(k, 0) for k in okeys) / st_) for i in back_ids] if st_ else []
            back = [(i, n, sh) for i, n, sh in back if sh >= BACK_SHARE]
            allowed = {t: sum((a["season"].get("o_" + k) or 0) for k in okeys) for t, a in agg.items()}
            rk = rank_map(allowed, False)  # 1st allows the least
            if label == "3PT Made":
                # Threes: the defense's 3-point percentage allowed over its last 5 games (the board's Opp 3PT% rank).
                rk = rank_map({t: a["l5"].get("o_p3") for t, a in agg.items() if a["l5"].get("o_p3") is not None}, False)
            orank = rk.get(opp["id"])
            lg_avg = sum(allowed.values()) / max(1, len(allowed))
            factor = min(1.15, max(0.85, allowed.get(opp["id"], lg_avg) / lg_avg)) if lg_avg else 1.0
            for pid, p in latest.items():
                if pid in injured:
                    continue
                hist = [val(g["teams"][tid]["players"][pid]) for g in gl if pid in g["teams"][tid].get("players", {})]
                mins = [g["teams"][tid]["players"][pid]["min"] for g in gl if pid in g["teams"][tid].get("players", {})]
                # Props only for players with a real role: 20+ minutes a game over the last 5.
                if len(hist) < 3 or sum(mins[-5:]) / len(mins[-5:]) < BB_MIN_MPG:
                    continue
                l5 = hist[-5:]
                avg5, season = sum(l5) / len(l5), sum(hist) / len(hist)
                if season < min_avg:
                    continue
                proj = (0.5 * avg5 + 0.5 * season) * factor
                line, opn = lines.get((market, pid)) or (None, None)
                score, side, prob = prop_score(label, proj, line, hist, BB_COUNT, BB_FLOOR)
                player_log("bb", gl, tid, pid)
                rows.append({"pid": pid, "open": opn, "cat": label, "player": p["name"], "full": p.get("full"), "team": team["abbr"],
                             "mpg": round(sum(mins[-5:]) / len(mins[-5:]), 1), "pos": p.get("pos") or "",
                             "opp": opp["abbr"], "oppName": opp["name"], "home": team["ha"] == "home", "oppRank": orank,
                             "matchup": round(10 * (orank - 1) / (nteams - 1), 1) if orank and nteams > 1 else None,
                             "l5": [round(x, 1) for x in reversed(l5)], "avg5": round(avg5, 1), "season": round(season, 1),
                             "games": len(hist), "proj": round(proj, 1), "game": eid, "line": line, "score": score,
                             "side": side, "prob": prob, "_hist": hist,
                             **({"teamOut": {"share": round(out_share, 2), "names": out_names[:3]}} if out_share >= TEAM_OUT else {}),
                             # Teammates returning (never the player himself: his own return doesn't shrink his share).
                             **({"bbBack": {"names": [n for i, n, _ in back if i != pid][:2], "share": round(sum(sh for i, _, sh in back if i != pid), 2)}}
                                if any(i != pid for i, _, _ in back) else {}),
                             **({"bbVac": {"share": round(vac_share, 2), "names": [n for n in gone_names.values() if n][:3]}}
                                if vac_share >= BB_VAC_MIN else {})})
    return rows


from datetime import date, timedelta
run_basketball("nba", "nba", date(2026, 10, 1), None)
run_basketball("mens-college-basketball", "cbb", date(2026, 11, 1), 50)

# --- pick analysis: Take large / Take / Take small / Fade / Pass with reasons --------------------
TAKES = ("Take", "Take small", "Take large")
STAKE = {"Take small": 0.5, "Take large": 2}  # units; a plain Take is 1
LARGE_BAR = 9.0  # a Take large needs at least this, with every signal agreeing (was 8.5: four Large totals on one night was too much risk)
LARGE_BAR_FB = 8.5  # football: a clean 8.5 is Large too, and from 9.0 one warning is allowed (Nov 6)
LARGE_STAT = 8.0  # ...and the stat matchup alone has to be this strong
INJ_SIDE_W = {"nba": 1.0, "cbb": 1.0}  # Nov 2: afternoon tweaks toward JD reverted after they cost the slate
#  # injuries in the basketball stat matchup (the price model counts them too)
DOG_ML_MAX = 200  # underdog prices where the moneyline follows the spread read
DOG_ML_GAP = 99.0  # (off) ...to within this much
INJ_MOVE = 999.0  # (off) line movement doesn't influence picks in any sport
PACE1_UNDER = 0.0  # (warning only) trim on an under against the #1 pace team
STRONG_READ_FB, SMALL_BELOW_FB = 8.0, 7.0  # football (Nov 6): a clean 7.0 is a full bet, and from 8.0 one warning keeps it full
STRONG_READ = 8.5  # from here up a single warning keeps a full-unit bet
SMALL_BELOW = 7.5  # game picks under this are half-unit Small bets: stake follows confidence
BB_PROP_SMALL_BELOW = 7.5  # basketball props: 7.0-7.4 small, 7.5+ regular (the 7-8 band backtested best)
PROP_SMALL_BELOW = 8.0  # props from the 7.5 bar up to this are Small bets
def ordn(n):
    if n is None:
        return "—"
    n = int(n)
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def pnum(v):
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return None


def fsp(v):
    return "—" if v is None else "PK" if v == 0 else (f"+{v:g}" if v > 0 else f"{v:g}")


BIG_SPREAD = {"cfb": 17, "nfl": 10, "nba": 12, "cbb": 14}
LOW_TOTAL = {"cfb": 47.5, "nfl": 41, "nba": 215, "cbb": 135}  # college unders in the 40s: teams can pop off
SPORT = {"cfb": "fb", "nfl": "fb", "nba": "bb", "cbb": "bb"}


def side_of(g, kind):
    pk = (g.get("pick") or {}).get(kind)
    if pk:
        return pk["side"]
    m = g.get("model") or {}
    if kind == "total":
        t = m.get("totalSide")
        return None if not t else ("over" if t.startswith("Over") else "under")
    t = m.get("spreadSide") if kind == "spread" else m.get("mlSide")
    return None if not t else ("home" if t.startswith(g["home"]["abbr"] + " ") else "away")


def kalshi_for(g, kind):
    k, s = g.get("kalshi") or {}, side_of(g, kind)
    if not s:
        return None
    return (k.get("total") or {}).get(s) if kind == "total" else (k.get("ml" if kind == "moneyline" else "spread") or {}).get(s)


def stat_edge(g, side):
    mine = theirs = 0
    best = None
    for sec in g.get("sections") or []:
        for row in sec["rows"]:
            name, a, b = row[0], row[1], row[2]
            ra, rh = a[4], b[0]
            if ra is None or rh is None or ra == rh or name in NEUTRAL_ROWS or name in ROW_W:
                continue  # pace is style and penalties are noise: neither belongs in this count
            my, op = (ra, rh) if side == "away" else (rh, ra)
            if my < op:
                mine += 1
                if not best or op - my > best[3]:
                    best = (name, my, op, op - my)
            else:
                theirs += 1
    return mine, theirs, best


def weather_of(g):
    w = str(g.get("weather") or "")
    rain = re.search(r"(\d+)% Rain", w)
    wind = re.search(r"(\d+)mph", w)
    return {"indoor": "Indoors" in w, "rain": int(rain.group(1)) if rain else 0, "wind": int(wind.group(1)) if wind else 0}


SM_SPREAD = {"cfb": (0.17, 0.008), "nfl": (0.2, 0.012), "nba": (0.2, 0.01), "cbb": (0.2, 0.006)}
ML_FAV_EXP = {"cfb": 0.3, "nfl": 0.2, "nba": 0.5, "cbb": 0.5}  # how hard a heavy moneyline price shrinks the score: -375 keeps 76%, -1000 keeps 57%
TOT_DEAD = float(os.environ.get("GP_TOTDEAD", 0.25))  # rank gap (as a share of the league) that is ignored
TOT_PTS = {"nfl": float(os.environ.get("GP_TOTPTS_NFL", 35)), "cfb": float(os.environ.get("GP_TOTPTS_CFB", 52.5))}  # points per unit of real gap
CFB_HOME_EDGE = 1.0  # extra strength toward the home team in college
NFL_HOME_EDGE = 1.0  # about 1.4 points once MARGIN_FIT scales it (2.0 had grown to 2.8 points after the Nov 4 margin fit, nearly a full field goal)
LETDOWN = 1.0  # strength taken off a team coming off a big upset win
HUGE_GAP = 0.55  # unit gap (share of the league) that counts as a massive mismatch
LINE_W = float(os.environ.get("GP_LINEW", 0.35))  # share of the spread score that comes from data-vs-line
BB_HOME_PTS = {"nba": 0.0, "cbb": 0.0}  # Nov 2: afternoon tweaks toward JD reverted after they cost the slate
#  # home-court points in the basketball stat matchup
ALIGN_W = 0.0  # (off) how far toward JD's scale (a slight shift)
ALIGN_BB = {lg: {"side": (4.41, 0.30), "total": (2.81, 0.50)} for lg in ("nba", "cbb")}  # JD ~ a + b * ours, Nov 2 NBA boards
# NBA: tried 1.25 on Nov 2, but the gap to the lines was a missing home-court edge, not scale (lines = 3.4 + 1.05 x data).
# Our stat margin put on the betting lines' scale (fit Nov 4: home line = a + b x our margin). Without it the
# line check saw every big line as too big (NBA, NFL) or too small (college), which a separate points cost used to mask.
SPREAD_DATA_W_LG = {"nba": 0.7}  # Nov 9: NBA only, 70% teams / 30% line  # (NFL had 0.35)  # NFL leans more on how lopsided the team data is (Nov 6)
SPREAD_DATA_W = float(os.environ.get("GP_DATAW_ALL", 0.6))  # Nov 6: spreads rate the teams first (60%) and the line second (40%); was 0.15 # every sport: share of the spread score from how lopsided the matchup is, so the line is measured a bit less
FB_DATA_W = 0.0  # (edge over the line only: best of the October backtests, 31-36 -4.6u vs -9.9u at 0.2)  # football spreads: share of the score from raw matchup strength (the rest is edge over the line)
SPREAD_WILD = {"nfl": 5.0, "cfb": 14.0}  # football: spread edges this big (points) are held to a Lean
CBB_VALUE_FIT = None  # set each build by fit_cbb_margins
MARGIN_FIT = {"cfb": (0.94, 0.75), "nfl": (0.38, 2.33), "nba": (0.0, 1.55), "cbb": (0.0, 1.0)}  # spread-matched (SD ratio), not a regression slope, which shrinks real disagreements
LINE_PTS = {"cfb": (2.0, 1.0), "nfl": (0.6, 0.7), "nba": (1.0, 1.4), "cbb": (1.0, 0.8)}  # (points per unit of strength, points of edge per score point)
SHARE_W = float(os.environ.get("GP_SHAREW", 4.0))  # weight of the row-by-row count (the board's ADV column)
L3_WEIGHT = float(os.environ.get("GP_L3W", 0.8))  # share of last-3 ranks vs full-season ranks in a matchup
REC_W = float(os.environ.get("GP_RECW", 1.0))  # record (win % gap) weight, college: a little (was 3.0, then 0)
REC_W_LG = {"nfl": float(os.environ.get("GP_RECW_NFL", 1.0))}  # NFL uses the college weights (Nov 6; was 1.5)
POW_W = float(os.environ.get("GP_POWW", 0.0))  # opponent-adjusted power gap weight (cap; college was 2.0: power-vs-line side covered 38%)
POW_W_LG = {"nfl": float(os.environ.get("GP_POWW_NFL", 0.0))}  # NFL uses the college weights (Nov 6; was 1.0)
SIDE_DEAD = {"nfl": float(os.environ.get("GP_SD_NFL", 0.15)), "nba": 0.1, "cbb": 0.08}  # rank gaps (share of the league) too small to matter on sides
POWER_SCALE = {"cfb": 21.0, "nfl": 10.0, "nba": 12.0, "cbb": 15.0}  # power gap that earns the full +3
SIDE_SCALE = {"cfb": 14, "nfl": float(os.environ.get("GP_SS_NFL", 18)), "nba": 12, "cbb": 12}  # rank-mismatch strength to a 0-10 score
TOT_BEST_W = 0.8  # how much an offense's best route to points (passing or rushing) counts over its other one
TOT_WILD = {"cfb": 8.0, "nfl": 6.0}  # points from the line where the market likely knows more than the stats
TOT_WILD_CAP = 6.0
TOT_EXTRA = {"cfb": 0.5, "nfl": 1.0}  # most that the price model can add to a football total (the NFL season model is steadier)
PROJ_W = {"cfb": 0.7, "nfl": 0.5}  # weight of the opponent-adjusted scoring projection vs the rank matchup in a total
MOVE_MIN = {"cfb": 3.0, "nfl": 1.5}  # line move from the open (points) that counts as real news
MOVE_W = 0.0  # line movement doesn't influence picks in any sport (was 0.25; shown on cards as information only)
SIT_ROWS = {name for name, _k, _hb in SIT}
NEUTRAL_ROWS = {"Pace", "Team Pace"}
FB_ML_MAX_FAV = -300  # football moneylines this short or shorter are never bets
ROAD_FAV_MAX = 14.0  # college: a road favorite laying this many or more is never a bet
NBA_TOTAL_SMALL_CUSHION = 3.0  # ...from 3 to 5 points of cushion an NBA total can still be a small bet (Nov 9)
NBA_TOTAL_CUSHION = 5.0  # NBA totals: projected combined scoring must clear the line by this much to be a bet (JD's rule)
FB_SIT_W = 0.5  # football situational rows (3rd downs, turnover margin, penalties) count half in the categories-won share
ROW_W = {"Penalties": 0.25, "Opp FT%": 0.25, "FT%": 0.5, "Fast Break": 0.25}  # weight in the "better in more stat categories" count
MISMATCH_UNITS = {"scoring", "passing", "rushing", "third downs"}
SHORT_ML = -400  # moneyline this short or shorter: prefer the spread
SHORT_ML_CAP = 5.5
SHORT_ML_LIFT = 0.75
KALSHI_AGAINST = 0.45  # Kalshi has to clearly lean the other way (under 45%) to hold a pick back
ELITE_D_RANK = 6  # NFL: a top-6 scoring defense (last 3 games) can carry an Under on its own (see one_elite_defense)
ELITE_D_PROJ = 2.0  # ...when the projection is at least this far under the total
STRENGTH_OVER_PROJ = 2.0  # college strength-matchup over: the projection must clear the total by this much (Nov 7: CLEM-SYR at +0.4; Oct overs 21-9 at 2+)
CFB_OVER_TRIM = 1.0  # college overs
VALUE_W_LG = {"cfb": 0.0, "nfl": 0.0}  # NFL follows college (Nov 6; was 5%)  # college: the price model has been on the wrong side (Oct backtest)
VALUE_W = 0.05  # price model (was 0.1, then halved again Nov 4) share of the score (was 0.2; Nov 3 backtest: 10% beat 20% in football and tied in basketball)
BB_MATCH_W = 6.0  # basketball totals: offense-vs-defense matchup, in score points per unit of gap
DEF_BATTLE = 0.8  # side strength kept when both scoring defenses are top quarter
LOW_TOTAL_FLOOR = 0.75  # sides shrink with the total (a 34.5 NFL total keeps 75%)
DEF_TOTAL_W = 1.0  # two top (or bottom) quarter defenses push a football total this much
INJ_TOTAL_W = 0.6  # share of a missing player's scoring a basketball total loses (was 0.6; the price model already subtracts injured players too)
CTX_CAP = 1.0  # most that defenses + pace together can move a football total
PACE_W = 0.6  # most pace can move a football total  # stat rows that are style, not quality
SM_TOTAL_UNIT = {"cfb": float(os.environ.get("GP_TU_CFB", 0.9)), "nfl": float(os.environ.get("GP_TU_NFL", 0.85)), "nba": 1.0, "cbb": 1.2}  # football totals toned down (were 0.65 / 1.8) after big overs kept missing  # NFL: 3 games is a small sample, so be less sensitive


UNIT_PAIRS = {
    "fb": [("PPG", "PPG/A", 2, "scoring"), ("Pass YPG", "PassYPG/A", 1, "passing"), ("Rush YPG", "RushYPG/A", 1, "rushing"),
           ("Pass TD/G", "Pass TD/A", 1, "passing TDs"), ("Rush TD/G", "Rush TD/A", 1, "rushing TDs"),
           ("Sacks Allow/G", "Sacks/G", 0.5, "pass protection"), ("Off 3rd %", "Def 3rd %", 0.5, "third downs")],  # situational: half weight
    "bb": [("PPG", "Opp PPG", 2, "scoring"), ("2PT%", "Opp 2PT%", 1, "2-point shooting"), ("3PT%", "Opp 3PT%", 1, "3-point shooting"),
           ("FT%", "Opp FT%", 0.2, "free throws"), ("Paint PPG", "Opp Paint PPG", 1, "paint scoring")],
}
DEF_NAME = {"scoring": "scoring defense", "passing": "pass defense", "rushing": "run defense", "passing TDs": "pass-TD defense",
            "rushing TDs": "run-TD defense", "pass protection": "pass rush", "third downs": "third-down defense",
            "2-point shooting": "2-point defense", "3-point shooting": "3-point defense", "free throws": "free-throw defense",
            "paint scoring": "paint defense"}


SOS_W = {"cfb": 0.25, "nfl": 0.25, "nba": 0.75, "cbb": 0.4}  # most strength the schedule can add or take (football's power rating already credits schedule)
SOS_SCALE = {"cfb": 12.0, "nfl": 6.0, "nba": 6.0, "cbb": 8.0}  # schedule gap (points per game) that earns the full weight
SOS_CAP = 0.35  # the schedule never moves the read by more than this share of what the stats show


def fastest_pace(g):
    """Teams in this game ranked #1 in recent pace (basketball boards' Team Pace L5 rank)."""
    r = next((r for sec in g.get("sections") or [] for r in sec["rows"] if r[0] == "Team Pace"), None)
    if not r:
        return []
    return [g[sd]["abbr"] for sd, rk in (("away", r[1][4]), ("home", r[2][0])) if rk == 1]


def stat_matchup(lg, g, nteams):
    """Data-driven confidence for each bet type, from offense-vs-defense unit matchups.

    Each team's offensive units are compared with the opposing defense's matching units (recent-game
    ranks). A strong offense meeting an equally strong defense cancels out; a strong offense meeting a
    weak defense is a mismatch. The spread version is helped by points received and hurt by big numbers
    laid; the total compares recent scoring (each offense against the other defense) with the posted total.
    """
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    tot = wsum = tot_both = wsum_both = max_gap = gap_a = gap_h = 0.0
    tot_gaps, tot_notes = {}, []
    edges = []  # (net, text, side)
    a_better = h_better = 0
    for r in R.values():
        if r[0] in NEUTRAL_ROWS:
            continue  # playing fast or slow isn't better or worse
        w = ROW_W.get(r[0], 1.0)  # penalties swing week to week, so they count for less
        if SPORT[lg] == "fb" and r[0] in SIT_ROWS:
            w *= FB_SIT_W
        ra, rh = r[1][4], r[2][0]
        if ra is not None and rh is not None:
            a_better += w * (ra < rh)
            h_better += w * (rh < ra)
    for off, dfn, wt, unit in UNIT_PAIRS[SPORT[lg]]:
        if off not in R or dfn not in R:
            continue
        ao, hd, ho, ad = R[off][1][4], R[dfn][2][0], R[off][2][0], R[dfn][1][4]
        if None in (ao, hd, ho, ad):
            continue
        away_mm, home_mm = (hd - ao) / nteams, (ad - ho) / nteams  # > 0: the offense out-ranks the defense it faces
        SR = g.get("seasonRanks") or {}
        if off in SR and dfn in SR and None not in (SR[off][0], SR[dfn][1], SR[off][1], SR[dfn][0]):
            # Mostly the last 3 games, with the full season mixed in so one hot or cold stretch can't carry the matchup.
            away_mm = L3_WEIGHT * away_mm + (1 - L3_WEIGHT) * (SR[dfn][1] - SR[off][0]) / nteams
            home_mm = L3_WEIGHT * home_mm + (1 - L3_WEIGHT) * (SR[dfn][0] - SR[off][1]) / nteams
        sdz = SIDE_DEAD.get(lg, 0.0)
        sd_ = lambda x: math.copysign(max(0.0, abs(x) - sdz), x)
        tot += wt * (sd_(away_mm) - sd_(home_mm))
        wsum += wt
        max_gap = max(max_gap, abs(away_mm), abs(home_mm))
        if unit in MISMATCH_UNITS:  # TDs and sacks swing too much to call a mismatch 'massive' on their own
            gap_a, gap_h = max(gap_a, away_mm), max(gap_h, home_mm)
        if unit != "pass protection":  # volume units: both offenses beating both defenses points to the over
            dz = lambda x: math.copysign(max(0.0, abs(x) - TOT_DEAD), x)  # only real gaps count, and by how big they are
            tot_gaps[unit] = (dz(away_mm), dz(home_mm), ao, hd, ho, ad)
        for mm, team, opp, o_rank, d_rank in ((away_mm, g["away"], g["home"], ao, hd), (home_mm, g["home"], g["away"], ho, ad)):
            if mm > 0.25:
                edges.append((mm, f"{team['abbr']} {unit} ({ordn(o_rank)}) vs {opp['abbr']} {DEF_NAME[unit]} ({ordn(d_rank)})",
                              "away" if team is g["away"] else "home"))
    # Totals: each offense scores through the air or on the ground, so yards and TDs are read together as one
    # route, and the offense is judged mostly by its best route (a run-first team with a bad passing game
    # still scores). Scoring and third downs count on their own.
    for i, sd in enumerate(("away", "home")):
        gv = {u: v[i] for u, v in tot_gaps.items()}
        routes = {}
        for name, yu, tu in (("passing", "passing", "passing TDs"), ("rushing", "rushing", "rushing TDs")):
            if yu in gv and tu in gv:
                routes[name] = (gv[yu] + gv[tu]) / 2
        part = 2 * gv.get("scoring", 0.0) + FB_SIT_W * gv.get("third downs", 0.0)
        wpart = (2 if "scoring" in gv else 0) + (FB_SIT_W if "third downs" in gv else 0)
        if routes:
            hi, lo = max(routes.values()), min(routes.values())
            part += 2 * len(routes) * (TOT_BEST_W * hi + (1 - TOT_BEST_W) * lo)
            wpart += 2 * len(routes)
            best = max(routes, key=routes.get)
            if routes[best] > 0.15:
                yu, tu = best, best + " TDs"
                v_y, v_t = tot_gaps[yu], tot_gaps[tu]
                o_y, d_y = (v_y[2], v_y[3]) if sd == "away" else (v_y[4], v_y[5])
                o_t, d_t = (v_t[2], v_t[3]) if sd == "away" else (v_t[4], v_t[5])
                opp = "home" if sd == "away" else "away"
                tot_notes.append((f"{g[sd]['abbr']} {best} ({ordn(o_y)} in yards, {ordn(o_t)} in TDs) vs {g[opp]['abbr']} "
                                  f"{'pass' if best == 'passing' else 'run'} defense ({ordn(d_y)}, {ordn(d_t)})", "over"))
            elif routes[best] < -0.15:
                tot_notes.append((f"{g[sd]['abbr']} has no clear way to move the ball against this defense", "under"))
        tot_both += part
        wsum_both = max(wsum_both, wpart)
    out = {}
    ln = g["lines"]
    if wsum:
        u = tot / wsum
        share = a_better / (a_better + h_better) if a_better + h_better else 0.5
        strength = SIDE_SCALE[lg] * u + SHARE_W * (share - 0.5)  # > 0 favors the away team; share = rows where the away team ranks better
        parts = {"units": round(SIDE_SCALE[lg] * u, 2), "share": round(SHARE_W * (share - 0.5), 2)}
        rec_notes = []
        sv = g.get("sosVal") or [None, None]
        if None not in sv:
            # Strength of schedule: stats piled up against weak opponents count for a bit less. It trims or adds to
            # the stat read but can never be more than SOS_CAP of it, so the data still leads.
            diff = sv[0] - sv[1]  # > 0: the away team's recent opponents were tougher
            adj = SOS_W[lg] * max(-1.0, min(1.0, diff / SOS_SCALE[lg]))
            adj = max(-SOS_CAP * abs(strength), min(SOS_CAP * abs(strength), adj))
            if abs(adj) >= 0.05:
                strength += adj
                parts["schedule"] = round(adj, 2)
            if abs(adj) >= 0.2:
                tough, soft = ("away", "home") if diff > 0 else ("home", "away")
                rec_notes.append((f"Schedule: {g[soft]['abbr']}'s recent opponents were weaker than {g[tough]['abbr']}'s "
                                  f"({sv[0 if soft == 'away' else 1]:+.1f} vs {sv[0 if tough == 'away' else 1]:+.1f} points per game), "
                                  f"so {g[soft]['abbr']}'s recent numbers count a bit less", tough))
        ia = g.get("injAdj")
        if ia and lg in LINE_PTS:
            # Injuries move the expected margin; convert those points into matchup strength.
            swing = (ia["home"]["off"] + ia["home"]["def"]) - (ia["away"]["off"] + ia["away"]["def"])  # > 0 helps the away team
            iw = INJ_SIDE_W.get(lg, 1.0)  # basketball: the price model already subtracts injured players' points
            strength += iw * swing / LINE_PTS[lg][0]
            parts["injuries"] = round(iw * swing / LINE_PTS[lg][0], 2)
            for sd in ("away", "home"):
                cost = ia[sd]["off"] + ia[sd]["def"]
                if cost >= 0.5:
                    who = ", ".join(f"{n} ({pos}, {st.lower()})" for n, pos, st, _, _ in ia[sd]["players"][:3])
                    rec_notes.append((f"Injuries cost {g[sd]['abbr']} about {cost:.1f} points: {who}", "home" if sd == "away" else "away"))
        if lg == "cfb" and not g.get("neutral"):
            # College road teams struggle ("you have to play a perfect game to win on the road"): lean home.
            strength -= CFB_HOME_EDGE
        if lg in BB_HOME_PTS and not g.get("neutral"):
            # Home court: the lines include it (about 3 points on the Nov 2 NBA slate), the stat comparison didn't.
            parts["home"] = round(-BB_HOME_PTS[lg] / LINE_PTS[lg][0], 2)
            strength -= BB_HOME_PTS[lg] / LINE_PTS[lg][0]
        if lg == "nfl" and not g.get("neutral"):
            parts["home"] = -NFL_HOME_EDGE
            strength -= NFL_HOME_EDGE  # home field is worth about a point and a half
        for sd, sgn in (("away", -1), ("home", 1)):
            if (g.get("letdown") or {}).get(sd):
                # Coming off a big upset win as a 7+ point underdog: the emotional letdown spot.
                strength += sgn * LETDOWN
                rec_notes.append((f"{g[sd]['abbr']} is coming off a big upset win: emotional letdown spot", "home" if sd == "away" else "away"))
        if SPORT[lg] == "fb":
            # Records and opponent-adjusted power (the team-rating model credits who each team has played).
            (wa, la), (wh, lh) = g.get("wl") or [[0, 0], [0, 0]]
            if wa + la and wh + lh:
                d = wa / (wa + la) - wh / (wh + lh)
                rw = REC_W_LG.get(lg, REC_W)
                strength += rw * d
                parts["records"] = round(rw * d, 2)
                if abs(d) >= 0.25:
                    better = g["away"] if d > 0 else g["home"]
                    rec_notes.append((f"Records: {g['away']['abbr']} {wa}-{la} vs {g['home']['abbr']} {wh}-{lh}",
                                      "away" if better is g["away"] else "home"))
            pw = g.get("power")
            if pw:
                gap = pw[0] - pw[1]  # points per game, adjusted for strength of schedule
                pwc = POW_W_LG.get(lg, POW_W)
                strength += max(-pwc, min(pwc, pwc * gap / POWER_SCALE[lg]))
                parts["power"] = round(max(-pwc, min(pwc, pwc * gap / POWER_SCALE[lg])), 2)
                if abs(gap) >= POWER_SCALE[lg] / 3:
                    better = g["away"] if gap > 0 else g["home"]
                    rec_notes.append((f"Adjusted for opponents played, {better['abbr']} rates {abs(gap):.1f} points per game better",
                                      "away" if gap > 0 else "home"))
        if SPORT[lg] == "fb":
            # Low-scoring games are closer to coin flips: fewer possessions, and one score decides it.
            dfr = R.get("PPG/A")
            if dfr and None not in (dfr[1][4], dfr[2][0]) and max(dfr[1][4], dfr[2][0]) <= nteams * 0.25:
                strength *= DEF_BATTLE
                rec_notes.append((f"Two top scoring defenses ({ordn(dfr[1][4])} and {ordn(dfr[2][0])}): a low-scoring game is closer to a coin flip",
                                  "away" if strength < 0 else "home"))
            tl = ln.get("totalNum")
            if tl and LG_TOTALS.get(lg):
                shrink = max(LOW_TOTAL_FLOOR, min(1.0, tl / LG_TOTALS[lg]))
                strength *= shrink
                parts["lowTotal"] = round(shrink, 2)
        side = "away" if strength > 0 else "home"
        base = min(10.0, abs(strength))
        if SPORT[lg] == "fb" and max_gap < TOT_DEAD:
            # No real mismatch anywhere (good meets good, bad meets bad): nothing to bet on the side.
            base = min(base, 3.9)
            rec_notes.append(("No unit has a big edge on either side: strengths cancel out", "away" if side == "home" else "home"))
        team = g[side]
        notes = [(t, sd) for _, t, sd in sorted(edges, reverse=True)[:3]] + rec_notes
        price = pnum(ln.get("homeML" if side == "home" else "awayML"))
        # Laying a big price has little value even when the team should win: shrink the moneyline score.
        ml = base * ((150 / abs(price)) ** ML_FAV_EXP[lg] if price is not None and price <= -150 else 1.0)
        if price is not None and price > 100:
            ml *= (100 / price) ** 0.5  # an underdog has to win outright: +150 keeps 82%, +300 keeps 58%
        out["moneyline"] = {"score": round(ml, 1), "side": side, "parts": parts, "call": f"{team['abbr']} {fmt_ml(price) if price else 'ML'}", "notes": notes}
        if ln.get("spreadNum") is not None:
            sp, sp_side, sp_notes = base, side, list(notes)
            if lg in LINE_PTS:
                # The line is charged once: turn the data's edge into an expected margin and score how far it beats
                # the spread. (Nov 4: this replaced a separate points-laid cost that charged the line a second time.)
                hfit, sfit = MARGIN_FIT.get(lg, (0.0, 1.0))
                margin_away = sfit * LINE_PTS[lg][0] * strength - hfit  # data's expected margin for the away team, on the lines' scale
                e_away = margin_away - ln["spreadNum"]          # beat the away line by this many points (home line = spreadNum)
                line_signed = max(-10.0, min(10.0, e_away / LINE_PTS[lg][1]))
                dw = SPREAD_DATA_W_LG.get(lg, SPREAD_DATA_W)  # a share for how lopsided the matchup is (the rest is edge over the line)
                blended = dw * min(10.0, base) * (1 if side == "away" else -1) + (1 - dw) * line_signed
                sp_side = "away" if blended > 0 else "home"
                sp = abs(blended)
                if SPORT[lg] == "fb" and max_gap < TOT_DEAD:
                    sp = min(sp, 3.9)  # no unit has a real edge on either side: not a spread bet
                fav = g["away"] if margin_away > 0 else g["home"]
                sp_notes.append((f"The data makes {fav['abbr']} about {abs(margin_away):.1f} points better; against the line that leaves "
                                 f"{abs(e_away):.1f} points of edge for {g['away']['abbr'] if e_away > 0 else g['home']['abbr']}",
                                 "away" if e_away > 0 else "home"))
            sl = ln["spreadNum"] if sp_side == "home" else -ln["spreadNum"]
            out["spread"] = {"score": round(max(0.0, min(10.0, sp)), 1), "side": sp_side, "call": f"{g[sp_side]['abbr']} {fsp(sl)}", "strength": strength,
                             "notes": sp_notes, "edgePts": round(abs(e_away), 1) if lg in LINE_PTS else None}
    off, dfn = R.get("PPG"), R.get("PPG/A") or R.get("Opp PPG")
    if off and dfn and ln.get("totalNum") is not None and None not in (off[1][3], off[2][1], dfn[1][3], dfn[2][1]):
        recent = (off[1][3] + dfn[2][1]) / 2 + (off[2][1] + dfn[1][3]) / 2  # last 3 (football) / last 5 (hoops)
        proj = recent
        if SPORT[lg] == "fb" and None not in (off[1][0], off[2][4], dfn[1][0], dfn[2][4]):
            # Football: last 3 games alone swing too much (a couple of blowouts), so blend in the full season and
            # weigh how the offenses match up with the defenses they face (both units beating bad defenses = over).
            # Opponent-adjusted: each team scores its average plus how far the defense it faces is from an average
            # defense, so an elite defense against a weak offense compounds instead of meeting halfway.
            half = LG_TOTALS.get(lg, 45.0) / 2
            adj = lambda o, d: max(3.0, o + d - half)
            recent = adj(off[1][3], dfn[2][1]) + adj(off[2][1], dfn[1][3])
            season = adj(off[1][0], dfn[2][4]) + adj(off[2][4], dfn[1][0])
            proj = (recent + season) / 2
            ia = g.get("injAdj")
            if ia:  # missing scorers lower the total, missing defenders raise it
                proj += -(ia["away"]["off"] + ia["home"]["off"]) + (ia["away"]["def"] + ia["home"]["def"])
            both = tot_both / wsum_both if wsum_both else 0.0
            # Matchup as points on top of an average game in this league, so it is judged against the line too:
            # "both offenses have the edge" is already priced in when the total is high.
            m_total = LG_TOTALS.get(lg, 45.0) + TOT_PTS[lg] * both
            if ia:
                m_total += -(ia["away"]["off"] + ia["home"]["off"]) + (ia["away"]["def"] + ia["home"]["def"])
            blend_total = PROJ_W[lg] * proj + (1 - PROJ_W[lg]) * m_total
            sig = (blend_total - ln["totalNum"]) / SM_TOTAL_UNIT[lg]
            far = abs(blend_total - ln["totalNum"])
            wild = False  # football (NFL Nov 5, college Nov 6): no Lean cap far from the market, a warning instead (see analyze_game)
            if far >= TOT_WILD[lg]:
                g.setdefault("_farTotal", round(far))
            dfr = R.get("PPG/A")
            def_note = []
            sig_before_ctx = sig  # defenses and pace are game context: together they can move the total at most CTX_CAP
            if dfr and None not in (dfr[1][4], dfr[2][0]):
                if max(dfr[1][4], dfr[2][0]) <= nteams * 0.25:
                    sig -= DEF_TOTAL_W
                    def_note = [(f"Both defenses are top-quarter in points allowed ({ordn(dfr[1][4])} and {ordn(dfr[2][0])})", "under")]
                elif min(dfr[1][4], dfr[2][0]) > nteams * 0.75:
                    sig += DEF_TOTAL_W
                    def_note = [(f"Both defenses are bottom-quarter in points allowed ({ordn(dfr[1][4])} and {ordn(dfr[2][0])})", "over")]
            pace = R.get("Pace")
            pace_note = []
            if pace and None not in (pace[1][4], pace[2][0]):
                # Pace nudges every total a little: two fast teams mean more plays, two slow ones fewer.
                p = ((pace[1][4] + pace[2][0]) / 2 - 1) / max(1, nteams - 1)  # 0 = fastest, 1 = slowest
                nudge = PACE_W * (1 - 2 * p)
                sig += nudge
                if abs(nudge) >= PACE_W / 2:
                    pace_note = [(f"Pace: {g['away']['abbr']} ranks {ordn(pace[1][4])} and {g['home']['abbr']} {ordn(pace[2][0])} in plays per game",
                                  "over" if nudge > 0 else "under")]
            # The scoring projection already reflects good defenses and slow offenses, so the context nudges can't stack.
            sig = sig_before_ctx + max(-CTX_CAP, min(CTX_CAP, sig - sig_before_ctx))
            if wild:
                pace_note = pace_note + [(f"Our number is {abs(blend_total - ln['totalNum']):.0f} points from the market: a gap that big usually "
                                          "means the line knows something (injuries, weather, a backup QB), so this stays a Lean at most",
                                          "under" if blend_total > ln["totalNum"] else "over")]
            notes = tot_notes + def_note + pace_note + [(f"Scoring projects to {proj:.1f} (last 3 games {recent:.1f}, full season {season:.1f}) against a total of {ln['totalNum']:g}",
                      "over" if proj > ln["totalNum"] else "under"),
                     (f"Offense-vs-defense gaps point to about {m_total:.1f} points (a typical game here is {LG_TOTALS.get(lg, 45.0):.1f})",
                      "over" if m_total > ln["totalNum"] else "under")]
        else:
            ia = g.get("injAdj")
            if ia:
                # Teammates take a missing scorer's shots, so a total loses only part of his scoring.
                proj -= INJ_TOTAL_W * (ia["away"]["off"] + ia["home"]["off"])
            sig = (proj - ln["totalNum"]) / SM_TOTAL_UNIT[lg]
            wild = False
            notes = [(f"Recent scoring (each offense vs the other defense) adds up to {proj:.1f} against a total of {ln['totalNum']:g}",
                      "over" if sig > 0 else "under")]
            # Both offenses have to show up for an over: each offense's unit gaps against the defense it faces,
            # with the weaker offense counting extra (one team stuck against a good defense drags the total).
            wts = {u: w for _, _, w, u in UNIT_PAIRS["bb"]}
            side_gap = []
            for i in (0, 1):
                ws = sum(wts[u] for u in tot_gaps if u in wts)
                side_gap.append(sum(wts[u] * v[i] for u, v in tot_gaps.items() if u in wts) / ws if ws else 0.0)
            ga, gh = side_gap
            sig += BB_MATCH_W * ((ga + gh) / 2 + 0.5 * min(ga, gh))
            for i, sd in enumerate(("away", "home")):
                gv = side_gap[i]
                opp = "home" if sd == "away" else "away"
                if gv <= -0.1:
                    notes.append((f"{g[sd]['abbr']} offense has no edge against the {g[opp]['abbr']} defense (scoring, shooting, paint)", "under"))
                elif gv >= 0.1:
                    notes.append((f"{g[sd]['abbr']} offense has the edge against the {g[opp]['abbr']} defense (scoring, shooting, paint)", "over"))
        side = "over" if sig > 0 else "under"
        out["total"] = {"score": round(min(10.0, abs(sig)), 1), "side": side, "proj": round(proj, 1), "wild": SPORT[lg] == "fb" and wild,
                        "call": f"{'Over' if side == 'over' else 'Under'} {ln['totalNum']:g}", "notes": notes}
    return out


def trend_records(g, kind):
    """Per-team betting-trend rates that apply to this game: (away, home), each a list of (label, rate, games)."""
    out = []
    sp = g["lines"].get("spreadNum")  # home line
    for side in ("away", "home"):
        b = g[side].get("bet") or {}
        venue = None if g.get("neutral") else ("Away" if side == "away" else "Home")
        if kind == "total":
            keys = [("ou", "O/U"), (f"ou{venue}", f"O/U {venue.lower()}")] if venue else [("ou", "O/U")]
        else:
            fav = sp is not None and ((sp > 0) if side == "away" else (sp < 0))
            dog = sp is not None and ((sp < 0) if side == "away" else (sp > 0))
            keys = [("ats", "ATS")] + ([(f"ats{venue}", f"ATS {venue.lower()}")] if venue else []) \
                + ([("atsFav", "ATS as favorite")] if fav else [("atsDog", "ATS as underdog")] if dog else [])
        rows = []
        for k, label in keys:
            r = b.get(k) or [0, 0, 0]
            if r[0] + r[1] >= 3:
                rows.append((label, r[0] / (r[0] + r[1]), r))
        out.append(rows)
    return out


def trend_nudge(g, kind):
    """Betting trends as a small tiebreaker (at most 0.6 of a point), signed toward away/over."""
    if kind == "moneyline":
        return 0.0
    a, h = trend_records(g, kind)
    lean = lambda rows: sum(r for _, r, _ in rows) / len(rows) - 0.5 if rows else 0.0
    if kind == "total":
        x = 2 * (lean(a) + lean(h)) / 2
    else:
        x = 1.5 * (lean(a) - lean(h))
    return max(-0.6, min(0.6, x))


def line_move(lg, g, kind):
    """Signal (+ = away/over) from how far the spread or total moved since it opened; football only."""
    if SPORT.get(lg) != "fb" or kind == "moneyline":
        return 0.0
    ln = g.get("lines") or {}
    op = ln.get("open") or {}
    try:
        if kind == "spread":
            now, was = ln.get("spreadNum"), float(op.get("homeSpread"))
            d = (now - was) if now is not None else 0.0  # > 0: the home line got worse, money on the away team
        else:
            now, was = ln.get("totalNum"), float(op.get("total"))
            d = (now - was) if now is not None else 0.0  # > 0: total went up, money on the over
    except (TypeError, ValueError):
        return 0.0
    if abs(d) < MOVE_MIN[lg]:
        return 0.0
    return math.copysign(min(MOVE_W, MOVE_W * abs(d) / (2 * MOVE_MIN[lg])), d)


def line_move_note(g, kind, mv):
    ln, op = g["lines"], g["lines"].get("open") or {}
    if kind == "spread":
        sd = "away" if mv > 0 else "home"
        return (f"Line moved from {g['home']['abbr']} {op.get('homeSpread')} to {fsp(ln['spreadNum'])} since it opened: "
                f"money or news is on {g[sd]['abbr']}", sd)
    return (f"Total moved from {op.get('total')} to {ln['totalNum']:g} since it opened", "over" if mv > 0 else "under")


def bad_team(g, side, n):
    """Football: the side we'd back is the worse team on both sides of the ball and in the standings, and its offense
    ranks in the bottom third in recent scoring. Points alone don't make a bad team a bet (Colts +6 vs Dallas, Nov 2026)."""
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    i, j = (1, 2) if side == "away" else (2, 1)
    rk = lambda name, k: (R[name][k][4] if k == 1 else R[name][k][0]) if name in R else None
    po, oo, pd, od = rk("PPG", i), rk("PPG", j), rk("PPG/A", i), rk("PPG/A", j)
    (wa, la), (wh, lh) = g.get("wl") or [[0, 0], [0, 0]]
    if None in (po, oo, pd, od) or not (wa + la and wh + lh):
        return None
    mine, theirs = (wa / (wa + la), wh / (wh + lh)) if side == "away" else (wh / (wh + lh), wa / (wa + la))
    if po > oo and pd > od and po > n * 2 / 3 and mine < theirs:
        t = g[side]["abbr"]
        return f"{t} is the worse team: scoring offense {ordn(po)}, scoring defense {ordn(pd)}, and the worse record. Getting points doesn't make a bad team a bet."
    return None


def dominant_side(g, n):
    """NFL: the team that out-ranks the other on both sides of the ball by a wide margin -- its scoring offense and
    scoring defense both in the top third (recent games), the opponent's both in the bottom third. JAX at BAL, Nov 5:
    offense 4th vs 26th defense, defense 10th vs 31st offense, JAX -8.5 won by 23."""
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    o, d = R.get("PPG"), R.get("PPG/A")
    if not o or not d or None in (o[1][4], o[2][0], d[1][4], d[2][0]):
        return None
    ao, ho, ad, hd = o[1][4], o[2][0], d[1][4], d[2][0]
    for sd, mo, md, oo, od in (("away", ao, ad, ho, hd), ("home", ho, hd, ao, ad)):
        if mo <= n / 3 and md <= n / 3 and oo > n * 2 / 3 and od > n * 2 / 3:
            return sd
    return None


DOMINANT_MAX_LAY = 13.5  # NFL: a dominant team is a full spread play laying up to this many points


def defense_battle(g, n):
    """Both scoring defenses rank in the top third in points allowed lately (football PPG/A over the last 3,
    basketball Opp PPG over the last 5): (away rank, home rank), or None."""
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    d = R.get("PPG/A") or R.get("Opp PPG")
    if not d or None in (d[1][4], d[2][0]):
        return None
    return (d[1][4], d[2][0]) if max(d[1][4], d[2][0]) <= n / 3 else None


def one_elite_defense(g, n):
    """NFL: one scoring defense ranks top 6 in points allowed over the last 3 games and the offense it faces is not
    top third in recent scoring (GB-NE, Nov 8: Packers 6th against the Patriots' 16th-ranked offense). (team, rank) or None."""
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    o, d = R.get("PPG"), R.get("PPG/A")
    if not o or not d or None in (o[1][4], o[2][0], d[1][4], d[2][0]):
        return None
    for dsd, drk, ork in (("away", d[1][4], o[2][0]), ("home", d[2][0], o[1][4])):
        if drk <= ELITE_D_RANK and ork > n / 3:
            return (g[dsd]["abbr"], drk)
    return None


def strength_matchups(g, n):
    """College football totals (Oct backtest, 183 games): each offense against the defense it faces, by recent scoring
    rank in thirds. ('under', ...) if a top-third offense meets a top-third defense (unders 31-18); ('over', ...) if a
    top-third offense meets a bottom-third defense (overs 27-13). Returns (side, text) or None."""
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    o, d = R.get("PPG"), R.get("PPG/A")
    if not o or not d or None in (o[1][4], o[2][0], d[1][4], d[2][0]):
        return None
    pairs = [(g["away"]["abbr"], o[1][4], g["home"]["abbr"], d[2][0]), (g["home"]["abbr"], o[2][0], g["away"]["abbr"], d[1][4])]
    good_good = [p_ for p_ in pairs if p_[1] <= n / 3 and p_[3] <= n / 3]
    good_bad = [p_ for p_ in pairs if p_[1] <= n / 3 and p_[3] > 2 * n / 3]
    if good_good and not good_bad:
        t, orr, dt, dr = good_good[0]
        return "under", f"{t}'s offense ({ordn(orr)} in scoring) runs into {dt}'s defense ({ordn(dr)}): a good offense meeting a good defense goes under (31-18 in our October data)."
    if good_bad and not good_good:
        t, orr, dt, dr = good_bad[0]
        return "over", f"{t}'s offense ({ordn(orr)} in scoring) faces {dt}'s defense ({ordn(dr)}): a good offense against a bad defense goes over (27-13 in our October data)."
    return None


def nba_bad_dog(g, side):
    """NBA: the side is the underdog getting 4 to 9.5 points and ranks worse than its opponent in both scoring offense
    and scoring defense (last 5). Those dogs covered 3-11 in the rebuilt games; under 4 points (5-4) or 10+ (3-1) the
    spread is reasonable and they can be bet (Nov 6). Returns a warning text or None."""
    hs = g["lines"].get("spreadNum")
    if hs is None or (hs < 0) == (side == "home") or hs == 0:
        return None  # not the underdog
    if not 4 <= abs(hs) < 10:
        return None  # a reasonable spread: bet it on the numbers
    R = {r[0]: r for sec in g.get("sections") or [] for r in sec["rows"]}
    o, d = R.get("PPG"), R.get("Opp PPG")
    if not o or not d or None in (o[1][4], o[2][0], d[1][4], d[2][0]):
        return None
    mo, md, oo, od = (o[1][4], d[1][4], o[2][0], d[2][0]) if side == "away" else (o[2][0], d[2][0], o[1][4], d[1][4])
    if mo > oo and md > od:
        return f"{g[side]['abbr']} is the underdog and worse on both ends (scoring {ordn(mo)} vs {ordn(oo)}, defense {ordn(md)} vs {ordn(od)}): getting {abs(hs):g} points, those dogs went 3-11."
    return None


def blend_confidence(lg, g, n):
    """Final confidence: the stat matchup sets the base, and the price model adds or subtracts.

    The price model (projected score vs the line, weighted against the market) can flip the side when
    its edge is bigger than what the stats show. Scores are 0-10: 7+ is a Play, 4+ a Lean.
    """
    sm = stat_matchup(lg, g, n)
    g["matchup"] = sm
    g["valueConf"] = g.get("confidence")
    if not g.get("confidence") or not g.get("model"):
        return
    m = g["model"]
    model_side = {"moneyline": side_from_text(g, m.get("mlSide")), "spread": side_from_text(g, m.get("spreadSide")),
                  "total": None if not m.get("totalSide") else ("over" if m["totalSide"].startswith("Over") else "under")}
    conf, pick = {}, {}
    for kind in ("moneyline", "spread", "total"):
        v = g["valueConf"][kind][0]
        vs = model_side[kind]
        st = sm.get(kind)
        pos = "away" if kind != "total" else "over"
        sig = 0.0
        if st:
            sig += st["score"] * (1 if st["side"] == pos else -1)
        if vs:
            sig += VALUE_W_LG.get(lg, VALUE_W) * v * (1 if vs == pos else -1)
        tr = trend_nudge(g, kind)  # betting trends: shown for information only, they don't move the score
        mv = line_move(lg, g, kind)  # a big move from the open: news or sharp money the stats can't see
        if mv:
            sig += mv
            if st:
                st.setdefault("notes", []).append(line_move_note(g, kind, mv))
        if kind == "total" and SPORT[lg] == "fb" and st and abs(sig) > st["score"] + TOT_EXTRA[lg]:
            # Football totals: the stats decide. The price model can add at most half a point on top, so they
            # can't lift a weak stat read into a Play.
            sig = math.copysign(st["score"] + TOT_EXTRA[lg], sig)
        cal = CAL.get((lg, kind), 1.0)  # learned from this league's graded picks of this bet type
        sig *= cal
        side = pos if sig > 0 else ("home" if kind != "total" else "under")
        x = abs(sig)
        # Soft ceiling above 7: stacked signals still read as a strong Play, but 10.0 means nearly everything agrees.
        score = round(x if x <= 7 else min(10.0, 7 + 3 * (1 - math.exp(-(x - 7) / 3))), 1)
        raw_score = score  # before the JD scale shift: the Large-bet check uses this
        if lg in ALIGN_BB:
            # Basketball scale nudged 35% toward JD's Profit Hunt boards (fit on the Nov 2 NBA slate): his reads sit nearer
            # the middle, so low and middle scores lift a little; totals above 8 ease back. Bets (6.3+) on sides are untouched.
            a, b = ALIGN_BB[lg]["total" if kind == "total" else "side"]
            score += ALIGN_W * max(0.0, a + b * score - score)
            if kind == "total" and score > 8:
                score = 8 + 0.6 * (score - 8)
            score = round(min(10.0, score), 1)
        if kind == "total" and SPORT[lg] == "bb" and side == "under" and fastest_pace(g):
            score = round(max(0.0, score - PACE1_UNDER), 1)  # JD: avoid unders against the #1 pace team
        ln = g["lines"]
        if kind == "total":
            call = f"{'Over' if side == 'over' else 'Under'} {ln['totalNum']:g}" if ln.get("totalNum") is not None else None
        elif kind == "spread":
            call = f"{g[side]['abbr']} {fsp(ln['spreadNum'] if side == 'home' else -ln['spreadNum'])}" if ln.get("spreadNum") is not None else None
        else:
            price = ln.get("homeML" if side == "home" else "awayML")
            call = f"{g[side]['abbr']} {price}" if price and price != "—" else None
        if not call:
            conf[kind] = [0.0, "Pass"]
            continue
        if kind == "moneyline" and (pnum(price) or 0) > 500:
            score = min(score, 5.0)  # long shots: cap at a lean
        if kind == "moneyline" and (pnum(price) or 0) <= -1000:
            conf[kind] = [0.0, "Pass"]  # too short to bet: risking 10+ to win 1
            pick[kind] = {"side": side, "call": call, "stat": st["score"] if st else None, "statSide": st["side"] if st else None,
                          "value": v, "valueSide": vs, "trend": 0.0, "tooShort": True}
            continue
        if kind == "spread" and SPORT[lg] == "fb" and st and st["side"] == side and (st.get("edgePts") or 0) >= SPREAD_WILD[lg]:
            st["wildFlag"] = True  # football (Nov 5-6): no cap, just the warning: 8.5+ stays a full bet, under that a small one
        if (kind == "spread" and lg == "nfl" and dominant_side(g, n) == side
                and (g["lines"].get("spreadNum") if side == "home" else -(g["lines"].get("spreadNum") or 0)) >= -DOMINANT_MAX_LAY):
            score = max(score, 7.5)  # the JAX-BAL profile: a full play even at a big number
            g["_dominant"] = side
        if kind == "spread" and lg == "nba" and nba_bad_dog(g, side):
            score = min(score, 6.9)  # with the warning below this is never a bet
        if kind == "spread" and SPORT[lg] == "fb" and bad_team(g, side, n):
            score = min(score, 6.9)  # with the warning below this is a Fade
        if lg == "nfl" and kind == "total" and side == "over":
            # The college over rules (Nov 6): a 1.0 trim from 6 up, and both offenses must rank in the top half in recent
            # scoring to be a bet (NYG-PHI over with the Eagles' offense 29th).
            if score >= 6:
                score = round(max(0.0, score - CFB_OVER_TRIM), 1)
            r_ = next((r for sec in g.get("sections") or [] for r in sec["rows"] if r[0] == "PPG"), None)
            if r_ and None not in (r_[1][4], r_[2][0]) and max(r_[1][4], r_[2][0]) > n / 2:
                score = min(score, 6.4)
                g["_overWeakO"] = max(r_[1][4], r_[2][0])
        if SPORT[lg] == "fb" and kind == "total" and side == "over":
            w_ = weather_of(g)
            if not w_["indoor"] and w_["rain"] >= 40:
                score = min(score, 6.4)  # rain: never bet the over (JD, Nov 5: "should we take the over in a rain game? No")
        if (lg == "cfb" and kind == "spread" and side == "away" and g["lines"].get("spreadNum") is not None
                and g["lines"]["spreadNum"] >= ROAD_FAV_MAX and not g.get("neutral")):
            g["_bigRoadFav"] = True  # big college road favorite: the upset spot, so a warning (a small bet unless the read is strong)
        if lg == "cfb" and kind == "total" and side == "over":
            # College overs (Oct backtest 4-10 at 7+): a 1.0 trim from 6 up, and both offenses must rank in the top
            # half in recent scoring to be a bet.
            if score >= 6:
                score = round(max(0.0, score - CFB_OVER_TRIM), 1)
            r_ = next((r for sec in g.get("sections") or [] for r in sec["rows"] if r[0] == "PPG"), None)
            if r_ and None not in (r_[1][4], r_[2][0]) and max(r_[1][4], r_[2][0]) > n / 2:
                score = min(score, 6.4)
        if lg == "nba" and kind == "total" and st and st.get("proj") is not None and g["lines"].get("totalNum") is not None:
            cush = (st["proj"] - g["lines"]["totalNum"]) * (1 if side == "over" else -1)
            if cush < NBA_TOTAL_SMALL_CUSHION:
                score = min(score, 6.9)  # a Lean at most; the warning below keeps it from being promoted
                st["thin"] = round(cush, 1)
            elif cush < NBA_TOTAL_CUSHION:
                g["_thinSmall"] = round(cush, 1)  # Nov 9: 3-5 points of cushion is a small bet (these leans went 5-1)
        sm_ = strength_matchups(g, n) if lg == "cfb" and kind == "total" else None
        if sm_ and sm_[0] == "over" and (lambda w_: not w_["indoor"] and w_["rain"] >= 40)(weather_of(g)):
            sm_ = None  # rain still rules out the over
        if sm_ and sm_[0] == side and st and st.get("side") == side and st.get("proj") is not None and g["lines"].get("totalNum") is not None \
                and (st["proj"] - g["lines"]["totalNum"]) * (1 if side == "over" else -1) > (STRENGTH_OVER_PROJ if side == "over" else 0):
            score = max(score, 7.0)  # college: a strength matchup with the projection on the same side is a play (Nov 6)
            g["_strength"] = sm_
        if lg != "cfb" and kind == "total" and side == "under" and st and st.get("side") == "under" and st.get("proj") is not None \
                and g["lines"].get("totalNum") is not None and st["proj"] < g["lines"]["totalNum"] and defense_battle(g, n):
            score = max(score, 7.0)  # two top-third defenses and a projection under the line: the Under is a play (Nov 6)
            g["_dBattle"] = defense_battle(g, n)
            st.pop("thin", None)  # the defenses carry it: no NBA cushion warning
        if lg == "nfl" and kind == "total" and side == "under" and st and st.get("side") == "under" and st.get("proj") is not None \
                and g["lines"].get("totalNum") is not None and g["lines"]["totalNum"] - st["proj"] >= ELITE_D_PROJ and one_elite_defense(g, n):
            # One elite scoring defense against an ordinary offense, with the projection 2+ under: a Lean, and a play
            # when the price model also has the Under by 2+ points (Nov 6).
            md_ = g.get("model") or {}
            agree = str(md_.get("totalSide", "")).startswith("Under") and (md_.get("totalEdge") or 0) >= 2
            score = max(score, 7.0 if agree else 6.0)
            g["_eliteD"] = one_elite_defense(g, n)
        if kind == "total" and st and st.get("wild") and st["side"] == side:
            score = min(score, TOT_WILD_CAP)  # far from the market: a Lean, never a Take
        if kind == "total" and SPORT[lg] == "bb":
            # The market moved the total 3+ points against us on injury news (BOS-HOU over, Nov 2): a Lean at most.
            op_t = pnum(((g["lines"].get("open") or {}).get("total")))
            ia_ = g.get("injAdj") or {}
            inj_pts = sum((ia_.get(sd_) or {}).get("off", 0) for sd_ in ("away", "home"))
            if op_t is not None and g["lines"].get("totalNum") is not None and inj_pts >= 2:
                mv_t = g["lines"]["totalNum"] - op_t
                if (side == "over" and mv_t <= -INJ_MOVE) or (side == "under" and mv_t >= INJ_MOVE):
                    score = min(score, 6.9)
                    if st is not None:
                        st.setdefault("notes", []).append((f"The total moved {op_t:g} to {g['lines']['totalNum']:g} on injury news ({inj_pts:.1f} points of players out): "
                                                           "the market has already priced it, so this stays a Lean", "under" if side == "over" else "over"))
        # (Removed Nov 3: the 6.9 cap when the price model disagreed. Graded picks went 9-3 on the stat side when it
        # disagreed by 1+ and 34-32 when it agreed, and the 4 capped picks all won. The price model still counts 20%.)
        conf[kind] = [score, (("Play " if score >= 7 else "Lean ") + call) if score >= 4 else "Pass"]
        pick[kind] = {"side": side, "call": call, "stat": st["score"] if st else None, "statSide": st["side"] if st else None,
                      "value": v, "valueSide": vs, "trend": round(tr if side == pos else -tr, 1), "cal": cal, "raw": raw_score}
    # Heavy favorite (-400 or shorter) with the spread on the same team: the spread is the bet. The moneyline risks
    # 4+ to win 1, so it stays a Lean, and the spread gets a lift because that's where the data edge gets paid.
    ml, sp = pick.get("moneyline"), pick.get("spread")
    if SPORT[lg] == "fb" and ml and sp and ml["side"] == sp["side"] and conf.get("spread", [0])[0] >= 4:
        price = pnum(g["lines"].get("homeML" if ml["side"] == "home" else "awayML"))
        if price is not None and price <= SHORT_ML:
            c_ml, c_sp = conf["moneyline"], conf["spread"]
            new_ml = min(c_ml[0], SHORT_ML_CAP)
            new_sp = round(min(10.0, c_sp[0] + SHORT_ML_LIFT), 1)
            conf["moneyline"] = [new_ml, ("Lean " + ml["call"]) if new_ml >= 4 else "Pass"]
            conf["spread"] = [new_sp, (("Play " if new_sp >= 7 else "Lean ") + sp["call"])]
            ml["preferSpread"] = sp["preferSpread"] = True
            st = (g.get("matchup") or {}).get("spread")
            if st is not None:
                st.setdefault("notes", []).append((f"At {fmt_ml(price)} the moneyline risks too much: the spread is the way to play this edge", sp["side"]))
    # Basketball small underdog the data has winning outright: the moneyline is at least close to the spread read
    # (JD's boards rate it that way, e.g. WSH +120 6.8 vs WSH +2.5 6.3).
    if SPORT[lg] == "bb" and ml and sp and ml["side"] == sp["side"]:
        price = pnum(g["lines"].get("homeML" if ml["side"] == "home" else "awayML"))
        if price is not None and 100 <= price <= DOG_ML_MAX and conf["spread"][0] - conf["moneyline"][0] > DOG_ML_GAP:
            new_ml = round(conf["spread"][0] - DOG_ML_GAP, 1)
            conf["moneyline"] = [new_ml, (("Play " if new_ml >= 7 else "Lean ") + ml["call"]) if new_ml >= 4 else "Pass"]
    g["confidence"], g["pick"] = conf, pick


def side_from_text(g, t):
    if not t:
        return None
    return "home" if t.startswith(g["home"]["abbr"] + " ") else "away"


def analyze_game(lg, g, kind, nteams):
    c = (g.get("confidence") or {}).get(kind)
    m, ln = g.get("model"), g["lines"]
    if not c or not m or g.get("state") != "pre":
        return None
    side = side_of(g, kind)
    if not side:
        return None
    score, call = c
    op = ln.get("open") or {}
    why, against, warn, tips = [], [], [], []
    k = kalshi_for(g, kind)
    if kind == "total":
        tot = m["away"] + m["home"]
        ((why if (tot > ln["totalNum"]) == (side == "over") else against)).append(f"Price model projects {tot:.1f} points against a total of {ln['totalNum']:g}.")
        rows = {r[0]: r for s in g.get("sections") or [] for r in s["rows"]}
        off, dfn = rows.get("PPG"), rows.get("PPG/A") or rows.get("Opp PPG")
        if off and dfn:
            oa, oh, da, dh = off[1][4], off[2][0], dfn[1][4], dfn[2][0]
            good_o = sum(1 for r in (oa, oh) if r is not None and r <= nteams / 3)
            bad_d = sum(1 for r in (da, dh) if r is not None and r >= 2 * nteams / 3)
            txt = f"Scoring offenses rank {ordn(oa)} and {ordn(oh)}, defenses {ordn(da)} and {ordn(dh)} in points allowed (recent games)."
            (why if (side == "over") == (good_o + bad_d >= 2) else against).append(txt)
        if op.get("total") and pnum(op["total"]) != ln["totalNum"]:
            up = ln["totalNum"] > pnum(op["total"])
            tips.append(f"Total moved {op['total']} → {ln['totalNum']:g} since it opened (shown for information; line moves don't affect our picks).")
        w = g.get("wx") or weather_of(g)
        if SPORT[lg] == "fb" and not w["indoor"] and (w["rain"] >= 40 or w["wind"] >= 15):
            wx = f"{w['rain']}% rain, {w['wind']} mph wind in the forecast"
            (warn.append(f"Weather: {wx} can slow scoring.") if side == "over" else why.append(f"Weather helps the under: {wx}."))
        if SPORT[lg] == "bb" and side == "under" and fastest_pace(g):
            warn.append(f"{' and '.join(fastest_pace(g))} {'plays' if len(fastest_pace(g)) == 1 else 'play'} the fastest pace in the league lately: extra possessions are risky for an under.")
        if g.get("_strength") and g["_strength"][0] == side:
            why.insert(0, g["_strength"][1])
        elif side == "under" and g.get("_dBattle"):
            da_, dh_ = g["_dBattle"]
            why.insert(0, f"Two top-third defenses ({ordn(da_)} and {ordn(dh_)} in points allowed lately) and our projection is under the line: the data points to a low score.")
        elif side == "under" and g.get("_eliteD"):
            t_, r_ = g["_eliteD"]
            why.insert(0, f"{t_} has a top scoring defense ({ordn(r_)} in points allowed lately) against an ordinary offense, and our projection is 2+ points under the line.")
        elif side == "under" and ln["totalNum"] <= LOW_TOTAL[lg]:
            warn.append(f"Low total ({ln['totalNum']:g}): one big quarter can bust an under.")
    else:
        team, opp = g[side], g["away" if side == "home" else "home"]
        proj = (m["home"] - m["away"]) if side == "home" else (m["away"] - m["home"])
        line_num = None if ln.get("spreadNum") is None else (ln["spreadNum"] if side == "home" else -ln["spreadNum"])
        if kind == "spread":
            (why if line_num is not None and proj + line_num > 0 else against).append(
                f"Price model has {team['abbr']} {'winning' if proj >= 0 else 'losing'} by {abs(proj):.1f}; the line is {team['abbr']} {fsp(line_num)}.")
        else:
            ph = m["pHome"] if side == "home" else 1 - m["pHome"]
            price = ln["homeML"] if side == "home" else ln["awayML"]
            imp = None if pnum(price) is None else (100 / (pnum(price) + 100) if pnum(price) > 0 else -pnum(price) / (-pnum(price) + 100))
            (why if imp is None or ph >= imp else against).append(f"Price model gives {team['abbr']} a {round(ph * 100)}% chance to win at {price}"
                + (f" (the price implies {round(imp * 100)}%)." if imp is not None else "."))
            if (pnum(price) or 0) >= 300:
                warn.append(f"Long shot ({price}): it loses most of the time even when the price is right.")
        mine, theirs, best = stat_edge(g, side)
        if mine + theirs:
            t = f"{team['abbr']} has the better recent rank in {mine} of {mine + theirs} stat categories" + (
                f"; biggest edge: {best[0]} ({ordn(best[1])} vs {ordn(best[2])})." if best else ".")
            (why if mine >= theirs else against).append(t)
        o_s = op.get("homeSpread" if side == "home" else "awaySpread") if kind == "spread" else op.get("homeML" if side == "home" else "awayML")
        c_s = line_num if kind == "spread" else pnum(ln["homeML"] if side == "home" else ln["awayML"])
        if o_s is not None and c_s is not None and pnum(o_s) is not None and pnum(o_s) != c_s:
            steam = c_s < pnum(o_s)
            ot = fsp(pnum(o_s)) if kind == "spread" else o_s
            ct = fsp(c_s) if kind == "spread" else fmt_ml(c_s)
            tips.append(f"Line moved {ot} → {ct} since it opened (shown for information; line moves don't affect our picks).")
        for t in injury_notes(g, side):
            warn.append(t)
        for p_ in ((g.get("inj") or {}).get(side) or []):
            if p_["pos"] == "QB" and p_["status"].lower() in ("questionable", "doubtful"):
                tips.append(f"{g[side]['abbr']} QB {p_['name']} is {p_['status'].lower()} (not counted unless he's ruled out).")
                break
        sp_m = (g.get("matchup") or {}).get("spread") or {}
        if kind == "spread" and sp_m.get("wildFlag") and sp_m.get("side") == side:
            warn.append(f"Our number is {sp_m['edgePts']:g} points from the line: a gap that big usually means the line knows something (a QB change, injuries, a team falling apart).")
        bt = bad_team(g, side, nteams) if kind == "spread" and SPORT[lg] == "fb" else nba_bad_dog(g, side) if kind == "spread" and lg == "nba" else None
        if bt:
            warn.append(bt)
        if lg in ("cfb", "cbb") and side == "away" and not g.get("neutral") and line_num is not None and line_num < 0:
            tips.append("Road favorite: worth knowing, but the data drives this pick.")
        if kind == "spread" and line_num is not None and abs(line_num) >= BIG_SPREAD[lg] and ((g.get("pick") or {}).get("spread") or {}).get("preferSpread"):
            tips.append(f"Big spread ({fsp(line_num)}), but at this moneyline price the spread is the better way to play it.")
        elif kind == "spread" and g.get("_dominant") == side:
            why.append(f"{g[side]['abbr']} out-ranks {g['home' if side == 'away' else 'away']['abbr']} on both sides of the ball by a wide margin (top-third offense and defense against bottom-third): the profile that covers big numbers.")
        elif kind == "spread" and line_num is not None and abs(line_num) >= BIG_SPREAD[lg]:
            warn.append(f"Big spread ({fsp(line_num)}): blowout margins are hard to predict, and the trailing team can cover late.")
        if kind == "spread" and SPORT[lg] == "fb" and line_num is not None:
            kn = next((x for x in (3, 7, 10) if abs(abs(line_num) - x) <= 0.5), None)
            if kn:
                tips.append(f"Near the key number {kn}: shop books for the best half-point.")
    if kind == "total" and lg == "cfb" and ln.get("totalNum") is not None:
        kt = next((x for x in (52, 56) if abs(ln["totalNum"] - x) <= 0.5), None)
        if kt:
            tips.append(f"Near the key total {kt}: shop for {kt} or buy the half-point so a {kt} pushes.")
    if k and k.get("p") is not None:
        if k["p"] >= 0.5:
            why.append(f"Kalshi prices this side at {round(k['p'] * 100)}% ({k['odds']}).")
        elif k["p"] >= KALSHI_AGAINST:
            tips.append(f"Kalshi prices this side at {round(k['p'] * 100)}% ({k['odds']}): about even.")
        else:
            against.append(f"Kalshi prices this side at {round(k['p'] * 100)}% ({k['odds']}), so the market leans the other way.")
    if m.get("trust") is not None and m["trust"] < 0.3:
        tips.append(f"Early season: the price model only counts {round(m['trust'] * 100)}% against the market with this few games played.")
    if kind != "moneyline":
        a_rows, h_rows = trend_records(g, kind)
        for tm_side, rows in (("away", a_rows), ("home", h_rows)):
            for label, rate, r in rows[1:]:  # the situational splits (venue, favorite/underdog)
                if abs(rate - 0.5) >= 0.2:
                    good = (rate > 0.5) == (side == "over" if kind == "total" else tm_side == side)
                    rec = f"{r[0]}-{r[1]}" + (f"-{r[2]}" if r[2] else "")
                    tips.append(f"Trend: {g[tm_side]['abbr']} {rec} {label} (shown for information; trends don't affect our picks).")
    sm = (g.get("matchup") or {}).get(kind)
    oppose = False
    if sm:
        for txt, sd in sm.get("notes") or []:
            (why if sd == side else against).append(txt + ".")
        if sm["side"] != side and sm["score"] >= 4:
            oppose = True
    if kind == "spread" and side == "away" and g.get("_bigRoadFav"):
        warn.append(f"Big road favorite ({fsp(-g['lines']['spreadNum'])}): college home teams pull these upsets, so the stake is smaller.")
    tm_ = (g.get("matchup") or {}).get("total") or {}
    if kind == "total" and side == "over" and g.get("_overWeakO"):
        warn.append(f"One offense ranks {ordn(g['_overWeakO'])} in recent scoring: an over needs both offenses in the top half.")
    if kind == "total" and g.get("_farTotal"):
        warn.append(f"Our number is {g['_farTotal']} points from the market: a gap that big can mean news the stats don't show.")
    if kind == "total" and tm_.get("thin") is not None:
        warn.append(f"Projected scoring is only {tm_['thin']:+.1f} points past the total: a total needs a {NBA_TOTAL_CUSHION:g}-point cushion to bet.")
    TAKE_BAR = (LEAN_BAR.get(lg) or {}).get("bar", LEAN_BAR_BASE)  # set from recent results (see lean_bars)
    if lg == "nfl":
        TAKE_BAR = min(TAKE_BAR, 6.0)  # NFL (Nov 6): a clean Lean from 6.0 is a small bet
    # Market signals (Kalshi, line moves) are shown but count lightly: only a clear Kalshi lean the other way
    # (under 45%) holds a pick back, and they don't count in the reasons-for vs reasons-against tally.
    market_no = bool(k and k.get("p") is not None and k["p"] < KALSHI_AGAINST)
    is_mkt = lambda t: t.startswith(("Kalshi prices", "Line moved", "Total moved"))
    n_why, n_against = sum(1 for t in why if not is_mkt(t)), sum(1 for t in against if not is_mkt(t))
    if score >= 7:
        # Sizing manages risk without capping plays: any warning (low total, big spread, key injury) makes it a half-unit bet.
        # When the data lines up strongly (8.5+), one warning (like a low total) is noted but doesn't halve the stake.
        fb_ = SPORT.get(lg) == "fb"
        strong = score >= (STRONG_READ_FB if fb_ else STRONG_READ) and len(warn) <= 1
        verdict = "Take small" if ((warn and not strong) or oppose or score < (SMALL_BELOW_FB if fb_ else SMALL_BELOW)) else "Take"
        pk = (g.get("pick") or {}).get(kind) or {}
        pk_raw = ((g.get("pick") or {}).get(kind) or {}).get("raw", score)  # the unshifted read (basketball scores are eased toward JD's scale)
        big = score  # every sport: the score shown on the card has to clear the Large bar (Nov 7: the WSH-ATL under went Large at 8.7 on a 9+ raw read)
        fb_ok = SPORT[lg] == "fb" and (big >= LARGE_BAR_FB and not warn or big >= LARGE_BAR and len(warn) <= 1)
        if (verdict == "Take" and kind != "moneyline" and (fb_ok or big >= LARGE_BAR and not warn) and not against
                and pk.get("statSide") == side and (pk.get("stat") or 0) >= LARGE_STAT
                and (pk.get("valueSide") in (None, side) or (pk.get("value") or 0) < 1)
                and not market_no):
            # Everything agrees: the stats are strong, the price model points the same way, no warnings.
            verdict = "Take large"
    elif score >= 4:
        # Not every game needs a play: a Lean becomes a Take only from 6.5 up (raised from 5 after a 42-play college Saturday), with nothing against it.
        verdict = "Take small" if (score >= TAKE_BAR and not warn and not oppose and not market_no and n_against <= n_why) else "Fade"  # a Lean that clears the bar: half a unit
    else:
        verdict = "Pass"
    if kind == "moneyline" and SPORT[lg] == "fb" and verdict.startswith("Take") and (pnum(ln.get("homeML" if side == "home" else "awayML")) or 0) <= FB_ML_MAX_FAV:
        # Heavy football favorites (-300 or shorter): risking 3+ to win 1 isn't a bet; the spread is the play.
        verdict = "Fade"
        against.append(f"Priced {ln.get('homeML' if side == 'home' else 'awayML')}: we don't bet football moneylines at -300 or shorter. Look at the spread instead.")
    if lg == "cbb" and verdict in ("Take", "Take large"):
        # College basketball starts at half stakes until its bets show an edge (30+ graded at 55%+ and in profit).
        n_, h_, u_ = (LEAN_BAR.get("cbb") or {}).get("takes", [0, None, 0])
        if not (n_ >= 30 and h_ is not None and h_ >= 0.55 and u_ > 0):
            verdict = "Take small"
            tips.append("College basketball bets are half a unit until they build a winning record (30+ bets at 55%+).")
    return {"verdict": verdict, "why": why, "against": against, "warn": warn, "tips": tips, "score": score,
            "call": re.sub(r"^(Play|Lean) ", "", call), "side": side}


AIR_CATS = {"Passing", "Receiving", "Receptions", "Rush + Rec"}
GROUND_CATS = {"Rushing"}


def analyze_prop(p, g=None, lg=None):
    if p.get("line") is None or p.get("score") is None:
        return None
    under = p.get("side") == "Under"
    won = sum(1 for v in p["l5"] if (v < p["line"] if under else v > p["line"]))
    why, against, warn = [], [], []
    (why if won >= 3 else against).append(
        f"Went {'under' if under else 'over'} {p['line']:g} in {won} of his last {len(p['l5'])} games (avg {p['avg5']:.1f}).")
    if p.get("matchup") is not None:
        (why if (p["matchup"] >= 5) != under else against).append(
            f"Opponent ranks {ordn(p['oppRank'])} in what it allows here (matchup {p['matchup']:.1f}/10).")
    if p.get("dkLine") is not None:
        why.append(f"No value at DraftKings' line ({p['dkLine']:g}), so this uses Kalshi's {p['line']:g} line, where the price leaves an edge. Bet it on Kalshi.")
    if p.get("kalshiP") is not None:
        why.append(f"Kalshi prices this at {round(p['kalshiP'] * 100)}% ({p['kalshiOdds']}); the model's edge over that is {p['edge'] * 100:.1f} points.")
    if p.get("flag"):
        warn.append("Line is far from his recent numbers: check injury or role news.")
    vc = p.get("vacated")
    if vc and lg in ("nfl", "cfb"):
        who = " and ".join(vc["names"][:2])
        kind = "carries" if p["cat"] in ("Rushing", "Rush + Rec") else "targets"
        if under:
            warn.append(f"{who} {'is' if len(vc['names']) == 1 else 'are'} out: about {vc['share']:.0%} of the team's recent {kind} go to the players left, which pushes this toward the over.")
        else:
            why.append(f"{who} {'is' if len(vc['names']) == 1 else 'are'} out, freeing about {vc['share']:.0%} of the team's recent {kind}: more volume for him.")
    rt = p.get("returning")
    if rt and lg in ("nfl", "cfb"):
        who = " and ".join(rt["names"][:2])
        kind = rt["kind"]
        if under:
            why.append(f"{who} {'is' if len(rt['names']) == 1 else 'are'} back from injury: his recent numbers came while they were out, so his {kind} should shrink.")
        else:
            warn.append(f"{who} {'is' if len(rt['names']) == 1 else 'are'} back from injury: his recent numbers came while they were out, so his {kind} should shrink.")
    bb_back = p.get("bbBack")
    if bb_back and lg in ("nba", "cbb"):
        txt = f"{' and '.join(bb_back['names'])} {'is' if len(bb_back['names']) == 1 else 'are'} back: about {bb_back['share']:.0%} of the team's {p['cat'].lower()} returns, so his share shrinks"
        (why if under else warn).append(txt + ".")
    bv = p.get("bbVac")
    if bv and lg in ("nba", "cbb"):
        who = " and ".join(bv["names"][:2]) or "Teammates"
        txt = f"{who} {'is' if len(bv['names']) == 1 else 'are'} out: about {bv['share']:.0%} of the team's recent {p['cat'].lower()} production is up for grabs"
        (warn if under else why).append(txt + (", which pushes this toward the over." if under else ": more volume for him."))
    if lg in ("nfl", "cfb") and under and p.get("l5"):
        avg_l5 = sum(p["l5"]) / len(p["l5"])
        if p["cat"] == "Passing" and p["line"] < LOW_PASS_LINE:
            # A QB touches the ball every play, so a low passing line is easy to clear (one garbage-time drive, or a
            # game where his team is chasing points). Only a QB who has really been struggling is an under here.
            if avg_l5 <= 0.85 * p["line"] and won >= 4:
                why.append(f"He has been struggling: {avg_l5:.0f} yards a game over his last {len(p['l5'])}, under this line in {won} of them.")
            else:
                warn.append(f"Low passing line ({p['line']:g}) for a starting QB: he touches the ball every play, so one garbage-time drive or a game where his team is chasing points clears it.")
        elif p["line"] < LOW_SKILL_LINE.get(p["cat"], 0):
            # Position players need the game plan and targets to come their way, so a low line is less of a giveaway.
            p["score"] = round(max(0.0, p["score"] - 0.5), 1)
            against.append(f"Low line ({p['line']:g}): one big play beats an under, so the score is trimmed slightly.")
    if lg in ("nfl", "cfb") and under and p["cat"] == "Receptions":
        # Catch-count unders went 1-4 in the first full slate (only the RB won): a receiver who gets 10+ targets
        # clears a small catch line even on a quiet day, so these get a small trim.
        p["score"] = round(max(0.0, p["score"] - 0.5), 1)
        against.append("Receptions unders lose to one extra target, so the score is trimmed slightly.")
    if lg in ("nba", "cbb") and under and p["cat"] in ("PRA", "Pts+Reb", "Reb+Ast") and p.get("pos") == "C":
        # A center's rebounds can explode (Clingan 18 boards on Nov 2), so his combo unders get the rebound trim.
        p["score"] = round(max(0.0, p["score"] - 0.5), 1)
        against.append("A center's rebounds can spike, so combo unders are trimmed slightly.")
    if lg in ("nba", "cbb") and under and p["cat"] == "Rebounds" and p["line"] >= REB_BIG_LINE:
        # Big men's rebound unders went 0-4 on Nov 1 (Queta, Sharpe, Jackson, Kessler): a missed-shot-heavy game
        # or extra minutes gives a center 3-4 extra boards fast.
        p["score"] = round(max(0.0, p["score"] - 0.5), 1)
        against.append("Rebound unders on bigs swing with missed shots and minutes, so the score is trimmed slightly.")
    pt = PROP_TUNE.get(lg) if lg in ("nba", "cbb") else None
    if pt and p.get("side") in ("Over", "Under"):
        nudge = pt["groups"].get(f"{p['cat']}|{p['side'].lower()}")
        if nudge:
            w_, l_ = pt["tally"][f"{p['cat']}|{p['side'].lower()}"]
            p["score"] = round(max(0.0, min(10.0, p["score"] + nudge)), 1)
            (why if nudge > 0 else against).append(
                f"{p['cat']} {p['side'].lower()}s have gone {w_}-{l_} lately, so the read is nudged {nudge:+.2g} (re-tuned every morning).")
    if p["games"] < 4:
        warn.append(f"Small sample: only {p['games']} games this season.")
    if lg in ("nba", "cbb") and p.get("mpg") is not None and p["mpg"] < BB_MIN_MPG:
        warn.append(f"Bench role ({p['mpg']:.0f} minutes a game): his minutes swing night to night.")
    if g is not None:
        w = g.get("wx") or weather_of(g)
        wet = SPORT.get(lg) == "fb" and not w["indoor"] and (w["rain"] >= 35 or w["wind"] >= 15)
        if wet and not under and p["cat"] in AIR_CATS:
            warn.append(f"Weather: {w['rain']}% rain, {w['wind']} mph wind outdoors can cut passing and catches.")
        if wet and not under and p["cat"] in GROUND_CATS:
            why.append(f"Weather ({w['rain']}% rain, {w['wind']} mph wind) tends to push teams toward the run.")
        n = (OUT_DATA.get((lg or "") + "_meta") or {}).get("teams") or 32
        if not under and p.get("oppRank") is not None and p["oppRank"] <= max(3, round(n * 0.1)) and p["cat"] not in ("Touchdowns", "FG Made"):
            warn.append(f"Facing a top-{p['oppRank']} defense in this category.")
        side = "home" if p.get("home") else "away"
        q = next((x for x in ((g.get("inj") or {}).get(side) or []) if x.get("id") == p.get("pid")), None)
        if q:
            warn.append(f"{p['player']} is listed {q['status'].lower()}.")
    need = len(p["l5"]) - 1 if len(p["l5"]) >= 3 else len(p["l5"])  # 4 of 5 (3 of 4, 2 of 3)
    three_of_five = (lg in ("nba", "cbb") and len(p["l5"]) >= 5 and won == need - 1 and p["score"] >= HIT_SMALL_SCORE)
    if p["cat"] != "Touchdowns" and p.get("line") is not None and p["l5"] and won < need and not three_of_five:
        against.append(f"Beat this line in only {won} of his last {len(p['l5'])} games: a bet needs {need} of {len(p['l5'])}.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif (p.get("edge") or 0) < (0.02 if p["cat"] == "Touchdowns" else PROP_EDGE_MIN) and (p["cat"] == "Touchdowns" or p.get("kalshiP") is not None):
        # The data likes it, but the price already says so: no value in betting it.
        against.append("The price already reflects what the data shows, so there's no value at this number."
                       if p["cat"] != "Touchdowns" else "The price already reflects the chance he scores, so there's no value at this number.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif lg in ("nfl", "cfb") and p["cat"] == "Receptions" and p["score"] < REC_PROP_BAR:
        against.append("Catch counts swing on one or two targets, so receptions need an 8.0+ read.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif (pnum(p.get("kalshiOdds")) or 0) <= PROP_MAX_JUICE:
        against.append(f"Price is too heavy ({p['kalshiOdds']}): laying that much juice eats the edge.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif lg in ("nba", "cbb") and p.get("teamOut") and p.get("side") == "Under" and p["cat"] != "3PT Made":
        to = p["teamOut"]
        against.append(f"{', '.join(to['names'][:2]) or 'Teammates'} out: about {to['share']:.0%} of the team's {p['cat'].lower()} is missing, "
                       "so more of it runs through him and unders are off (SGA, Dosunmu, Clingan on Nov 2).")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif lg in ("nba", "cbb") and p["cat"] == "Assists" and p.get("side") == "Over" and (p["line"] <= LOW_AST_LINE or p.get("pos") == "C"):
        against.append("Assist overs need a real distributor: not a center, and not a 1-2 assist line.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif lg in ("nba", "cbb") and p.get("kalshiP") is None and p.get("line") is not None and (p.get("edge") or 0) <= 0:
        # The line sits so far from his form that the model gives itself no weight: the book likely knows about a
        # role or minutes change, or the price is heavily juiced (lines like o0.5). Not a bet without an edge.
        against.append("The line is far from his usual numbers, so the model can't claim an edge: likely role news or a heavy price.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif lg in ("nba", "cbb") and (p["cat"], p.get("side")) not in BB_GOOD_PROPS:
        against.append("Not a prop type we bet: 3-pointer overs and rebound unders lost in our backtest.")
        verdict = "Fade" if p["score"] >= 4 else "Pass"
    elif lg in ("nba", "cbb") and p["score"] < (PROP_TUNE.get(lg) or {}).get("bar", NBA_PROP_BAR):
        verdict = "Fade" if p["score"] >= 4 else "Pass"  # basketball props: 7.0+
    elif lg in ("nba", "cbb") and p.get("mpg") is not None and p["mpg"] < BB_PROP_MPG and p["score"] < BB_PROP_BAR_LOWMIN:
        against.append(f"Plays {p['mpg']:.0f} minutes a game: under 30 minutes needs a {BB_PROP_BAR_LOWMIN:g}+ read.")
        verdict = "Fade"
    elif lg in ("nfl", "cfb") and p["score"] < BB_PROP_BAR:
        verdict = "Fade" if p["score"] >= 4 else "Pass"  # NBA and NFL props need a 7.5+ read to be a play
    elif lg in ("nfl", "cfb") and p["line"] < NFL_PROP_FLOOR.get(p["cat"], 0):
        against.append(f"Backup-sized line ({p['line']:g}): small roles swing too much to bet.")
        verdict = "Fade"
    elif p["score"] >= 7 and warn and p["score"] < PROP_WARN_BAR:
        verdict = "Fade"  # a warning (bench role, injury tag, tough defense) needs a really strong read to still bet
    elif p["score"] >= 7:
        verdict = "Take small" if warn or p["score"] < (BB_PROP_SMALL_BELOW if lg in ("nba", "cbb") else PROP_SMALL_BELOW) else "Take"  # just over the bar: half a unit
        if verdict == "Take" and p["score"] >= 9 and won >= 4 and not against and (p.get("edge") or 0) >= 0.05:
            verdict = "Take large"  # strong form, a good matchup and a real edge over the price
    elif p["score"] >= 4:
        verdict = "Fade"  # props need a 7.0+ read to be a play
    else:
        verdict = "Pass"
    tips_ = []
    if lg in ("nba", "cbb") and p["cat"] == "3PT Made" and p.get("side") == "Under" and verdict in ("Take", "Take large"):
        # Nov 9: 3-pointer unders are 8-11 live (-6.3u): one extra make decides them, so they're small bets for now.
        verdict = "Take small"
        warn.append("3-pointer unders swing on one made shot (8-11 live so far), so they're small bets for now.")
    if three_of_five and won < need and verdict.startswith("Take"):
        # Nov 9: strong reads held back only by a 3-of-5 hit rate went 13-5 as leans; they're small bets now.
        verdict = "Take small"
        warn.append(f"Beat this line in {won} of his last {len(p['l5'])} games (a full bet needs {need}): a small bet.")
    if p["cat"] == "Touchdowns" and verdict.startswith("Take"):
        # Nov 8: anytime touchdowns are too volatile to bet (one goal-line carry decides them); still rated, never a play.
        verdict = "Fade"
        against.append("Anytime touchdowns aren't bets on this board: one goal-line carry decides them, so they're too volatile.")
    if lg in ("nba", "cbb") and g is not None and p.get("side") == "Over" and verdict in ("Take", "Take large"):
        hs = (g.get("lines") or {}).get("spreadNum")
        team_line = None if hs is None else (hs if p.get("home") else -hs)
        if team_line is not None and team_line <= -BLOWOUT_FAV:
            # Nov 8 backtest: overs on a team favored by 10+ went 7-10 (starters sit in blowouts: SGA, 29 minutes in a 39-point win).
            verdict = "Take small"
            warn.append(f"His team is favored by {abs(team_line):g}: in a blowout the starters sit, which cuts into an over.")
    if lg in ("nba", "cbb") and verdict in ("Take", "Take large") and (PROP_TUNE.get(lg) or {}).get("half", True):
        # NBA props hit 48% over the first 87 graded (Oct 31-Nov 2): half-unit stakes until they show an edge
        # (30+ graded bets at 55%+ and in profit, checked every morning).
        verdict = "Take small"
        tips_.append("Basketball props are half-unit bets until the model shows an edge over a bigger sample.")
    return {"verdict": verdict, "why": why, "against": against, "warn": warn, "tips": tips_}


BLOWOUT_FAV = 10.0  # basketball: overs on a player whose team is favored by this many or more are small bets
HIT_SMALL_SCORE = 7.5  # basketball props that beat the line in 3 of 5 can be small bets from this score up (Nov 9)
PROP_MAX_JUICE = -150  # props priced at -150 or shorter are not bets
LOW_AST_LINE = 2.5  # assist overs at or below this line are not bets
PROP_WARN_BAR = 8.5  # a prop with a warning stays a (small) Take only from here up


def one_prop_per_player(g):
    """A player's props move together (one bad night sinks his points, PRA and Pts+Reb at once), so only his
    best-scoring prop stays a Take; the rest become Fades."""
    best = {}
    for p in g.get("props") or []:
        a = p.get("analysis")
        if a and a["verdict"].startswith("Take"):
            k = p.get("pid")
            if k not in best or p["score"] > best[k]["score"]:
                best[k] = p
    for p in g.get("props") or []:
        a = p.get("analysis")
        if a and a["verdict"].startswith("Take") and best.get(p.get("pid")) is not p:
            a["verdict"] = "Fade"
            a["against"].append(f"Already playing his {best[p['pid']]['cat']} prop: one bet per player, since his numbers move together.")


MAX_3PT_TAKES = 2  # per game: threes rise and fall together on a hot or cold shooting night


def cap_threes(g):
    """3-pointer props in one game move together (pace, shooting luck, a blowout): only the best two stay Takes, and at most one per team."""
    tk = sorted([p for p in g.get("props") or [] if p["cat"] == "3PT Made" and (p.get("analysis") or {}).get("verdict", "").startswith("Take")],
                key=lambda p: -p["score"])
    kept, teams = 0, set()
    for p in tk:
        if p["team"] in teams:
            # Two shooters on one team ride the same shooting night, pace and perimeter defense: one bet, not two.
            p["analysis"]["verdict"] = "Fade"
            p["analysis"]["against"].append(f"Already a 3-pointer play on {p['team']}: teammates' threes rise and fall together.")
        elif kept >= MAX_3PT_TAKES:
            p["analysis"]["verdict"] = "Fade"
            p["analysis"]["against"].append("Already two 3-pointer plays in this game: threes rise and fall together on one shooting night.")
        else:
            kept += 1
            teams.add(p["team"])


REC_OVER_CATS = ("Receiving", "Receptions")


def cap_receiving(key, g):
    """Football: pass-catching overs on one team all need the same thing (that team throwing a lot and completing it),
    and they split the same targets. Only the strongest stays a play per team; the rest are not bets (Nov 8: DET-MIA,
    Dulcich and Washington overs lost together when Miami threw for little)."""
    if SPORT.get(key) != "fb":
        return
    best = {}
    for p in g.get("props") or []:
        a = p.get("analysis") or {}
        if p.get("cat") in REC_OVER_CATS and p.get("side") == "Over" and str(a.get("verdict", "")).startswith("Take"):
            best.setdefault(p["team"], []).append(p)
    for team, ps in best.items():
        ps.sort(key=lambda p: -(p.get("score") or 0))
        for p in ps[1:]:
            p["analysis"]["verdict"] = "Fade"
            p["analysis"]["against"].append(f"Already a receiving over on {team} ({ps[0]['player']}): they need the same passing day and split the same targets.")


def hold_early(g):
    """College basketball before both teams have played CBB_MIN_GAMES: the confidence read shows, but nothing is a play."""
    n_ = g["early"]
    note = f"Early season: a team here has played {n_} game{'s' if n_ != 1 else ''}. Plays start once both teams have {CBB_MIN_GAMES}."
    for k, a in (g.get("analysis") or {}).items():
        if a and str(a.get("verdict", "")).startswith("Take"):
            a["verdict"] = "Fade"
        if a and a.get("verdict") in ("Fade", "Pass") and note not in a.get("against", []):
            a.setdefault("against", []).append(note)
        c = (g.get("confidence") or {}).get(k)
        if c and c[1].startswith("Play "):
            c[1] = "Lean " + c[1].split(" ", 1)[1]
    for p in g.get("props") or []:
        a = p.get("analysis")
        if a and str(a.get("verdict", "")).startswith("Take"):
            a["verdict"] = "Fade"
            a.setdefault("against", []).append(note)


def fit_cbb_margins(n):
    """College basketball: spread-match the price model's and the stat read's margins to the day's betting lines
    (slope = SD ratio, intercept = the average gap: mostly home court the ratings miss early in the season)."""
    global CBB_VALUE_FIT
    games = [g for g in OUT_DATA.get("cbb") or [] if g.get("state") == "pre" and g.get("model") and g["model"].get("src") != "torvik"
             and g["lines"].get("spreadNum") is not None]
    if len(games) < 20:
        return
    Y = np.array([-g["lines"]["spreadNum"] for g in games])  # market's expected home margin
    X = np.array([g["model"]["home"] - g["model"]["away"] for g in games])
    if X.std() > 0:
        b_ = float(Y.std() / X.std()); CBB_VALUE_FIT = (float(Y.mean() - b_ * X.mean()), b_)
    st_ = []
    for g in games:
        o = stat_matchup("cbb", g, n)
        sp_ = (o.get("spread") or {}).get("strength")
        if sp_ is not None:
            st_.append((g["lines"]["spreadNum"], sp_))
    if len(st_) >= 20:
        Ys, Xs = np.array([y for y, _ in st_]), np.array([x for _, x in st_])  # away line vs away strength
        if Xs.std() > 0:
            sf = float(Ys.std() / (LINE_PTS["cbb"][0] * Xs.std()))
            MARGIN_FIT["cbb"] = (float(-(Ys.mean() - sf * LINE_PTS["cbb"][0] * Xs.mean())), sf)
    print("cbb margin fit: value", CBB_VALUE_FIT, "stat", MARGIN_FIT["cbb"], file=sys.stderr)


def attach_analysis():
    for key in ("cfb", "nfl", "nba", "cbb"):
        n = (OUT_DATA.get(key + "_meta") or {}).get("teams") or 1
        if key == "cbb":
            fit_cbb_margins(n)
        for g in OUT_DATA.get(key) or []:
            if (g["lines"].get("filled") and SPORT[key] == "bb") or key == "cbb":
                value_conf_bb(key, g)  # lines arrived after the slate was built, or college margins re-fit to the market
            blend_confidence(key, g, n)
            a = {kind: analyze_game(key, g, kind, n) for kind in ("moneyline", "spread", "total")}
            g["analysis"] = {k2: v for k2, v in a.items() if v}
            for p in g.get("props") or []:
                p["analysis"] = analyze_prop(p, g, key)
            one_prop_per_player(g)
            cap_threes(g)
            cap_receiving(key, g)
            for sd_, spot_ in ((g.get("b2b") or {}).items() if SPORT[key] == "bb" else []):
                if spot_:
                    tonight_ = "on the road" if sd_ == "away" else "at home"
                    for k_ in ("spread", "moneyline", "total"):
                        a_ = (g.get("analysis") or {}).get(k_)
                        if a_ is not None:
                            a_.setdefault("tips", []).append(
                                f"{g[sd_]['abbr']} played yesterday ({'on the road' if spot_ == 'road' else 'at home'}) and plays {tonight_} tonight: second night of a back-to-back (shown for information; the lines already price it).")
            tv_ = g.get("torvik")
            if tv_:
                fav_ = g["home"] if tv_["margin"] >= 0 else g["away"]
                for k_, txt_ in (("spread", f"Torvik's ratings: {fav_['abbr']} by {abs(tv_['margin']):.1f}."),
                                 ("total", f"Torvik's ratings project {tv_['total']:.0f} points.")):
                    a_ = (g.get("analysis") or {}).get(k_)
                    if a_ is not None:
                        a_.setdefault("tips", []).append(txt_)
            if g.get("early") is not None:
                hold_early(g)
            ts_ = (g.get("analysis") or {}).get("total")
            if g.get("_thinSmall") is not None and ts_ and str(ts_.get("verdict", "")).startswith("Take"):
                ts_["verdict"] = "Take small"
                ts_.setdefault("warn", []).append(f"Projected scoring is {g['_thinSmall']:+.1f} points past the total: under the 5-point cushion, so a small bet.")
            apply_pick_overrides(key, g)
            same_game_cap(key, g)
        if SPORT[key] == "fb":
            top_large(key)


def top_large(key, big_slate=20):
    """Football: Large bets are the picks that meet the Large requirements (analyze_game). On a big slate (20+ games
    that day) with none, the single most data-backed pick is Large: score 8.0+ with a 9+ stat read, ranked by nothing
    against it, then fewest warnings, then score (Nov 6). Small slates get one only if a pick qualifies on its own."""
    by_day, games, has_large = {}, {}, set()
    for g in OUT_DATA.get(key) or []:
        if g.get("state") != "pre":
            continue
        day = g["date"].split(" · ")[0]
        games[day] = games.get(day, 0) + 1
        for k in ("spread", "total"):
            a = (g.get("analysis") or {}).get(k)
            pk = (g.get("pick") or {}).get(k) or {}
            if not a:
                continue
            if a.get("verdict") == "Take large":
                has_large.add(day)
            if a.get("verdict", "").startswith("Take") and (a.get("score") or 0) >= 8.0 and (pk.get("stat") or 0) >= 9.0 \
                    and pk.get("statSide") == a.get("side"):
                by_day.setdefault(day, []).append((len(a["against"]), len(a["warn"]), -a["score"], id(a), a))
    for day, rows in by_day.items():
        if games.get(day, 0) >= big_slate and day not in has_large:
            rows.sort(key=lambda r: r[:4])
            rows[0][-1]["verdict"] = "Take large"


def same_game_cap(key, g):
    """A spread and a total in one game that ride the same idea (the favorite plus the under: the underdog's offense is
    held down; the underdog plus the over: it shows up) win or lose together. Keep the stronger play (fewer warnings,
    the price model on its side, then the higher score) and make the other a small bet (Nov 7)."""
    an, pk = g.get("analysis") or {}, g.get("pick") or {}
    sp, tt = an.get("spread"), an.get("total")
    if not sp or not tt or not str(sp.get("verdict", "")).startswith("Take") or not str(tt.get("verdict", "")).startswith("Take"):
        return
    hs = g["lines"].get("spreadNum")
    if hs is None or hs == 0:
        return
    fav = "home" if hs < 0 else "away"
    # NBA (Nov 8): any spread and total in the same game, whichever way they point, keep only one full play.
    if key != "nba" and not ((sp.get("side") == fav and tt.get("side") == "under") or (sp.get("side") != fav and tt.get("side") == "over")):
        return
    def strength(kind, a):
        p_ = pk.get(kind) or {}
        price_against = p_.get("valueSide") not in (None, a.get("side")) and (p_.get("value") or 0) >= 1
        return (len(a.get("warn") or []), price_against, -(a.get("score") or 0))
    keep, cut = ("spread", "total") if strength("spread", sp) <= strength("total", tt) else ("total", "spread")
    a = an[cut]
    a["verdict"] = "Take small"
    a.setdefault("warn", []).append(f"Same game as the {an[keep].get('call') or keep} play"
                                    + (": a half stake keeps one game from swinging the day." if key == "nba" else ", and both need the same thing to happen: a half stake keeps one game from swinging the day."))


def apply_pick_overrides(key, g):
    """Hand-set verdicts (build/pick_overrides.json), graded like any other pick. Nothing on the card marks them.
    Each entry expires after its 'until' date (ET)."""
    try:
        rows = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pick_overrides.json")))
    except (OSError, ValueError):
        return
    today = datetime.now(ET).strftime("%Y-%m-%d")
    gm = f"{g['away']['abbr']}@{g['home']['abbr']}"
    for o in rows:
        if o.get("league") != key or o.get("game") != gm or o.get("until", "9999") < today:
            continue
        if o["kind"] == "prop":  # a player prop: matched by player id and category; only the verdict changes
            for p_ in g.get("props") or []:
                if str(p_.get("pid")) == str(o.get("pid")) and p_.get("cat") == o.get("cat") and p_.get("analysis"):
                    p_["analysis"]["verdict"] = o["verdict"]
            continue
        a = (g.get("analysis") or {}).get(o["kind"])
        if not a or a.get("side") != o.get("side"):
            continue  # the model is on the other side now: leave it alone
        a["verdict"] = o["verdict"]
        if o["verdict"].startswith("Take"):
            a["score"] = max(a.get("score") or 0, 7.0)
        c = (g.get("confidence") or {}).get(o["kind"])
        if c and o["verdict"].startswith("Take"):
            c[0] = max(c[0], 7.0)
            c[1] = "Play " + c[1].split(" ", 1)[1] if c[1].startswith(("Lean ", "Play ")) else c[1]
        elif c:  # not a bet: shown as a Lean at most
            c[0] = min(c[0], 6.9)
            a["score"] = min(a.get("score") or 0, 6.9)
            c[1] = "Lean " + c[1].split(" ", 1)[1] if c[1].startswith(("Lean ", "Play ")) else c[1]
        if c and o.get("score") is not None:  # a set score (e.g. a Pass shown at its data read)
            c[0] = a["score"] = float(o["score"])
            if o["verdict"] == "Pass":
                c[1] = "Pass"


# --- model picks history: hypothetical P/L of every green (Take) pick ---------------------
PROP_VALUE = {
    "Passing": lambda p: p.get("passYds", 0), "Rushing": lambda p: p.get("rushYds", 0), "Receiving": lambda p: p.get("recYds", 0),
    "Receptions": lambda p: p.get("rec", 0), "Rush + Rec": lambda p: p.get("rushYds", 0) + p.get("recYds", 0),
    "Touchdowns": lambda p: p.get("rushTD", 0) + p.get("recTD", 0), "Sacks": lambda p: p.get("sacks", 0),
    "Tackles + Ast": lambda p: p.get("tackles", 0), "FG Made": lambda p: p.get("fg", 0),
    "Points": lambda p: p.get("pts", 0), "Rebounds": lambda p: p.get("reb", 0), "Assists": lambda p: p.get("ast", 0),
    "PRA": lambda p: p.get("pts", 0) + p.get("reb", 0) + p.get("ast", 0), "Pts+Reb": lambda p: p.get("pts", 0) + p.get("reb", 0),
    "Pts+Ast": lambda p: p.get("pts", 0) + p.get("ast", 0), "Reb+Ast": lambda p: p.get("reb", 0) + p.get("ast", 0),
    "3PT Made": lambda p: p.get("tpm", 0), "Blk+Stl": lambda p: p.get("stl", 0) + p.get("blk", 0),
}


def game_day(g):
    now = datetime.now(ET)
    d = datetime.strptime(g["date"].split(" · ")[0] + f" {now.year}", "%b %d %Y")
    if d.month < 6 and now.month > 8:
        d = d.replace(year=now.year + 1)
    return d.strftime("%Y-%m-%d")


def pick_from_game(lg, g, kind, a):
    ln, m = g["lines"], g["model"]
    base = {"date": game_day(g), "time": g["date"].split(" · ")[1].replace(" ET", ""), "league": lg,
            "matchup": f"{g['away']['abbr']} @ {g['home']['abbr']}", "book": ln.get("book") or "DK"}
    side = a["side"]
    if kind == "moneyline":
        return {**base, "play": f"{g[side]['abbr']} ML", "price": pnum(ln["homeML" if side == "home" else "awayML"]),
                "grade": {"league": lg, "game": g["id"], "kind": "ml", "side": side}}
    if kind == "spread":
        line = ln["spreadNum"] if side == "home" else -ln["spreadNum"]
        return {**base, "play": g["pick"]["spread"]["call"], "price": pnum(ln["homeSpreadOdds" if side == "home" else "awaySpreadOdds"]) or -110,
                "grade": {"league": lg, "game": g["id"], "kind": "spread", "side": side, "line": line}}
    return {**base, "play": g["pick"]["total"]["call"], "price": pnum(ln["overOdds" if side == "over" else "underOdds"]) or -110,
            "grade": {"league": lg, "game": g["id"], "kind": "total", "side": side, "line": ln["totalNum"]}}


def grade_pick(pk):
    gr = pk["grade"]
    f = FIN.get(gr["league"], {}).get(gr["game"])
    if not f:
        return None
    sign = lambda x: "W" if x > 0 else "L" if x < 0 else "P"
    if gr["kind"] == "ml":
        return sign(f["home"] - f["away"] if gr["side"] == "home" else f["away"] - f["home"])
    if gr["kind"] == "spread":
        return sign((f["home"] - f["away"] if gr["side"] == "home" else f["away"] - f["home"]) + gr["line"])
    if gr["kind"] == "total":
        t = f["home"] + f["away"]
        return sign(t - gr["line"] if gr["side"] == "over" else gr["line"] - t)
    pl = f["players"].get(gr["pid"])
    if pl is None:
        return "V"  # did not play: void, stake returned
    v = PROP_VALUE[gr["cat"]](pl)
    return sign(v - gr["line"] if gr["side"] == "over" else gr["line"] - v)


FROZEN = ("lines", "confidence", "pick", "valueConf", "analysis", "matchup", "model", "kalshi", "props", "sections", "away", "home",
          "strip", "offRank", "defRank", "overallRank", "seasonRanks", "power", "wl", "injAdj", "inj")


def update_history(path_in, path_out):
    hist, snaps = {}, {}
    if path_in and os.path.exists(path_in):
        try:
            with open(path_in) as f:
                saved = json.load(f)
            hist = {p["id"]: p for p in saved.get("picks", [])}
            snaps = saved.get("pregame") or {}
        except (ValueError, KeyError, AttributeError):
            hist, snaps = {}, {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    # Freeze each game at kickoff: once a game starts, the board keeps its last pregame lines, scores and
    # verdicts instead of recomputing them from live odds. Snapshots older than a week are dropped.
    keep_from = (datetime.now(ET) - timedelta(days=7)).strftime("%Y-%m-%d")
    for lg in ("cfb", "nfl", "nba", "cbb"):
        for g in OUT_DATA.get(lg) or []:
            key = f"{lg}-{g['id']}"
            if g.get("state") == "pre":
                snaps[key] = {"at": now, "day": game_day(g), **{k: g[k] for k in FROZEN if k in g}}
            elif key in snaps:
                g.update({k: v for k, v in snaps[key].items() if k in FROZEN})
                g["frozen"] = snaps[key]["at"]
    snaps = {k: v for k, v in snaps.items() if v.get("day", "") >= keep_from}
    added = 0
    for lg in ("cfb", "nfl", "nba", "cbb"):
        for g in OUT_DATA.get(lg) or []:
            if g.get("state") != "pre":
                continue
            for kind, a in (g.get("analysis") or {}).items():
                pid = f"{lg}-{g['id']}-{kind}"
                take = a["verdict"] in TAKES
                # Every 4+ pick is logged so results can be broken out by confidence; the model P&L counts
                # only Takes. A pick logged as a Fade that later turns into a Take is re-logged at the new price.
                old = hist.get(pid)
                # Passes (under 4) are logged too, so the results table can show whether passing was right.
                if old and not old.get("result") and old.get("verdict") in TAKES and not take:
                    old["verdict"], old["score"], old["stake"] = a["verdict"], a["score"], 1  # dropped before kickoff
                # Until tip-off a bet follows the board: a size change (regular <-> large/small) or a new line or side
                # re-logs it, so the P&L and Top Picks match what the game page shows.
                follow = (take and old and not old.get("result") and old.get("verdict") in TAKES
                          and (old.get("verdict") != a["verdict"] or (kind != "moneyline" and old.get("play") != (g.get("pick") or {}).get(kind, {}).get("call"))))
                if not old or (take and old.get("verdict") not in TAKES) or follow:
                    try:
                        pk = pick_from_game(lg, g, kind, a)
                    except (KeyError, TypeError):
                        continue
                    if pk.get("price") is None:
                        continue
                    hist[pid] = {"id": pid, **pk, "verdict": a["verdict"], "score": a["score"],
                                 "stake": STAKE.get(a["verdict"], 1), "result": None, "logged": now}
                    if BACKFILL and lg in ("cfb", "nba"):
                        hist[pid]["backfilled"] = True
                    added += 1
            # A logged prop whose player has dropped off the board before tip-off (ruled out, or under the
            # minutes floor) is no longer a play.
            here = {f"{lg}-{g['id']}-{p.get('pid')}-{re.sub('[^A-Za-z0-9]', '', p.get('cat') or '')}" for p in g.get("props") or []}
            for old in hist.values():
                gr = old.get("grade") or {}
                if (old.get("league") == lg and gr.get("game") == g["id"] and gr.get("kind") == "prop" and not old.get("result")
                        and old.get("verdict") in TAKES and old["id"] not in here and not BACKFILL):
                    old["verdict"], old["stake"], old["dropped"] = "Fade", 1, True
            for p in g.get("props") or []:
                a = p.get("analysis")
                ppid = f"{lg}-{g['id']}-{p.get('pid')}-{re.sub('[^A-Za-z0-9]', '', p.get('cat') or '')}"
                old = hist.get(ppid)
                if a and old and not old.get("result") and old.get("verdict") in TAKES and a["verdict"] not in TAKES:
                    old["verdict"], old["stake"] = a["verdict"], 1  # dropped before kickoff
                if BACKFILL or not a or a["verdict"] not in TAKES or p["score"] < 7 or p.get("flag"):
                    continue  # (a rebuilt day has no live prop prices, so its props are not logged)
                pid = f"{lg}-{g['id']}-{p['pid']}-{re.sub('[^A-Za-z0-9]', '', p['cat'])}"
                if pid in hist:
                    o = hist[pid]
                    if not o.get("result") and o.get("verdict") in TAKES and o.get("verdict") != a["verdict"]:
                        o["verdict"], o["score"], o["stake"] = a["verdict"], p["score"], STAKE.get(a["verdict"], 1)
                    continue
                text = f"{p['player']} anytime TD" if p["cat"] == "Touchdowns" else f"{p['player']} {'u' if p['side'] == 'Under' else 'o'}{p['line']:g} {p['cat']}"
                hist[pid] = {"id": pid, "date": game_day(g), "time": g["date"].split(" · ")[1].replace(" ET", ""), "league": lg,
                             "matchup": f"{g['away']['abbr']} @ {g['home']['abbr']}", "play": text,
                             "price": pnum(p.get("kalshiOdds")) or -110, "book": "Kalshi fair" if p.get("kalshiOdds") else "DK (−110 assumed)",
                             "grade": {"league": lg, "game": g["id"], "kind": "prop", "pid": p["pid"], "cat": p["cat"],
                                       "side": "under" if p["side"] == "Under" else "over", "line": p["line"]},
                             "verdict": a["verdict"], "score": p["score"], "stake": STAKE.get(a["verdict"], 1),
                             "result": None, "logged": now}
                added += 1
    graded = 0
    for pk in hist.values():
        if not pk.get("result"):
            r = grade_pick(pk)
            if r:
                pk["result"], pk["gradedAt"] = r, now
                graded += 1
    picks = sorted(hist.values(), key=lambda p: (p["date"], p["id"]))
    OUT_DATA["modelHistory"] = picks
    if path_out:
        with open(path_out, "w") as f:
            json.dump({"updated": now, "picks": picks, "pregame": snaps}, f, separators=(",", ":"))
    print("history", len(picks), "picks;", added, "new;", graded, "graded", file=sys.stderr)


def build_recap():
    """One recap per sport, for its last day with graded model picks: results, what went wrong, and calibration."""
    OUT_DATA["recap"] = {"leagues": {}}
    for lg in ("cfb", "nfl", "nba", "cbb"):
        r = league_recap(lg)
        if r:
            OUT_DATA["recap"]["leagues"][lg] = r


def league_recap(lg):
    hist = [p for p in OUT_DATA.get("modelHistory") or [] if p["league"] == lg]
    graded = [p for p in hist if p.get("result")]
    if not graded:
        return None
    day = max(p["date"] for p in graded)
    passes = [p for p in graded if p["date"] == day and p["score"] < 4 and p["grade"]["kind"] != "prop"]
    dayp = [p for p in graded if p["date"] == day and p["score"] >= 4]
    units = lambda p: (p.get("stake") or 1) * (0 if p["result"] in ("P", "V") else -1 if p["result"] == "L" else
                                               (p["price"] / 100 if p["price"] > 0 else 100 / -p["price"]))
    rec = lambda ps: {"w": sum(p["result"] == "W" for p in ps), "l": sum(p["result"] == "L" for p in ps),
                      "p": sum(p["result"] in ("P", "V") for p in ps), "u": round(sum(units(p) for p in ps), 2)}
    takes = [p for p in dayp if p["verdict"] in TAKES and p["grade"]["kind"] != "ml"]
    name = {"ml": "Moneylines", "spread": "Spreads", "total": "Totals", "prop": "Props"}
    groups = {}
    for p in dayp:
        groups.setdefault(p["grade"]["kind"], []).append(p)
    wrong, right = [], []
    for kind, ps in sorted(groups.items()):
        r = rec(ps)
        if r["w"] + r["l"] < 3:
            continue
        hi = [p for p in ps if p["score"] >= 7]
        rh = rec(hi)
        if r["u"] <= -1.5:
            extra = f"; the 7+ ones went {rh['w']}-{rh['l']}" if len(hi) >= 2 and rh["u"] < 0 else ""
            wrong.append(f"{name[kind]} went {r['w']}-{r['l']} ({r['u']:+.1f}u){extra}.")
        elif r["u"] >= 1.5:
            right.append(f"{name[kind]} went {r['w']}-{r['l']} ({r['u']:+.1f}u).")
    hi, lo = [p for p in dayp if p["score"] >= 7], [p for p in dayp if 4 <= p["score"] < 7]
    rh, rl = rec(hi), rec(lo)
    if len(hi) >= 4 and len(lo) >= 4 and rh["w"] / max(1, rh["w"] + rh["l"]) < rl["w"] / max(1, rl["w"] + rl["l"]):
        wrong.append(f"Higher scores did worse: 7+ picks went {rh['w']}-{rh['l']} while 4-7 Leans went {rl['w']}-{rl['l']}.")
    fades = [p for p in dayp if p["verdict"] == "Fade"]
    rf = rec(fades)
    if rf["w"] + rf["l"] >= 3:
        (right if rf["l"] > rf["w"] else wrong).append(
            f"Faded Leans went {rf['w']}-{rf['l']} on their own side, so fading them was {'right' if rf['l'] > rf['w'] else 'a mistake'}.")
    rp = rec(passes)
    if rp["w"] + rp["l"] >= 5:
        hit = rp["w"] / (rp["w"] + rp["l"])
        (right if hit <= 0.55 else wrong).append(
            f"Passes (under 4) went {rp['w']}-{rp['l']} on their lean side, so {'sitting them out was right' if hit <= 0.55 else 'some of them were worth playing'}.")
    fmt = lambda p: f"{p['matchup']} {p['play']} ({p['score']:.1f}, {p['price']:+g})"
    misses = sorted([p for p in takes if p["result"] == "L"], key=lambda p: -p["score"])[:3]
    hits = sorted([p for p in takes if p["result"] == "W"], key=lambda p: -p["score"])[:3]
    changes = []
    for (clg, kind), m in sorted(CAL.items()):
        if clg == lg and abs(m - 1) >= 0.02:
            w, l, u = CAL_TALLY.get(f"{clg}-{kind}", [0, 0, 0.0])
            effect = ("borderline Plays drop to Leans and borderline Leans to Passes" if m < 1
                      else "strong Leans can reach Play")
            changes.append(f"{kind.capitalize()}s x{m:.2f} ({w}-{l}, {u:+.1f}u graded so far): a 7.0 now reads {7 * m:.1f}, so {effect}")
    plan = []
    tk = rec(takes)
    if tk["w"] + tk["l"]:
        hr = tk["w"] / (tk["w"] + tk["l"])
        plan.append(f"Bets went {tk['w']}-{tk['l']} ({hr:.0%}, {tk['u']:+.1f}u). Break-even at -110 is {BREAKEVEN:.1%}, so "
                    + ("that's a winning day." if tk["u"] > 0.25 else "that's a losing day." if tk["u"] < -0.25 else "that's about break-even."))
    sk = [p for p in dayp if p["verdict"] not in TAKES and p["grade"]["kind"] != "ml"]
    rs = rec(sk)
    if rs["w"] + rs["l"]:
        plan.append(f"Skipped picks would have gone {rs['w']}-{rs['l']} ({rs['u']:+.1f}u at 1 unit): "
                    + ("money left on the table." if rs["u"] > 0.5 else "sitting them out saved money." if rs["u"] < -0.5 else "about even."))
    lb = LEAN_BAR.get(lg)
    if lb:
        t3, s3 = lb["takes"], lb["skipped"]
        move = "lowered" if lb["bar"] < LEAN_BAR_BASE else "raised" if lb["bar"] > LEAN_BAR_BASE else "kept"
        plan.append(f"Lean-to-Take bar {move} {'to' if move != 'kept' else 'at'} {lb['bar']:.2f} for today: over three weeks {lb['why']}.")
    pa = PRICE_AGREE.get(lg)
    if pa:
        fmt_ = lambda r: f"{r[0]}-{r[1]}" + (f" ({r[0] / (r[0] + r[1]):.0%})" if sum(r) else "")
        plan.append(f"Price model check (3 weeks): when it agreed with the stats, picks went {fmt_(pa.get('agrees', [0, 0]))}; "
                    f"when it disagreed by a point or more, the stat side went {fmt_(pa.get('disagrees', [0, 0]))}. It counts 5% of the score, "
                    "and its weight comes down if disagreement keeps going our way.")
    pt = PROP_TUNE.get(lg)
    if pt:
        n_, h_, u_ = pt["bets"]
        rec_txt = f"{n_} graded prop bets over three weeks" + (f" ({h_:.0%}, {u_:+.1f}u)" if n_ else "")
        nud = [f"{k.replace('|', ' ')}s {v:+.2g} ({pt['tally'][k][0]}-{pt['tally'][k][1]})" for k, v in sorted(pt["groups"].items())]
        plan.append(f"Player props for today: bet from {pt['bar']:.2f} up ({pt['why']}); "
                    + ("stakes stay at half a unit until props prove an edge (30+ bets at 55%+). " if pt["half"] else "props are back to full stakes. ")
                    + f"Based on {rec_txt}." + (" Prop types nudged: " + "; ".join(nud) + "." if nud else ""))
    plan.append("The aim is profit, not volume: more plays only when the extra ones have been winning, and fewer when bets slip under break-even.")
    return {"day": day, "takes": rec(takes), "all": rec(dayp), "passes": rp, "wrong": wrong, "right": right, "plan": plan,
            "misses": [fmt(p) for p in misses], "hits": [fmt(p) for p in hits], "calibration": changes,
            "note": "" if changes else "No changes yet: calibration needs 12 graded picks per bet type, and it moves slowly."}

# --- data check: cross-check ESPN against independent sources -------------------------------
UA = {"User-Agent": "GushersPicks/1.0 (personal sports dashboard; data cross-check)"}


def fetch_text(url, headers=None, cache_name=None):
    path = os.path.join(CACHE, cache_name) if cache_name else None
    if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return open(path).read()
    try:
        req = urllib.request.Request(url, headers={**UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=45) as r:
            txt = r.read().decode("utf-8", "replace")
        if path:
            open(path, "w").write(txt)
        return txt
    except Exception as e:
        print("check fetch failed", url.split("?")[0], safe_error(e), file=sys.stderr)
        LAST_ERR[url.split("/")[2]] = safe_error(e)
        return None


LAST_ERR = {}  # host -> last error, shown in the data-check summary


def safe_error(e):
    """A short error description that can't leak credentials (no header values or URLs with keys)."""
    code = getattr(e, "code", None)
    if code:
        return f"HTTP {code}" + {401: " (the API key was rejected)", 403: " (access denied)", 429: " (rate limited)"}.get(code, "")
    if isinstance(e, ValueError) and "header" in str(e).lower():
        return "the API key is malformed (check for extra spaces or line breaks)"
    return type(e).__name__


def as_list(x, host):
    """API lists come back as JSON arrays; anything else is an error message worth showing."""
    if isinstance(x, list):
        return x
    if isinstance(x, dict) and x:
        msg = str(x.get("message") or x.get("error") or "unexpected response")
        LAST_ERR[host] = re.sub(r"[A-Za-z0-9+/=_-]{20,}", "[hidden]", msg)[:120]
    return []


def fetch_json(url, headers=None, cache_name=None):
    t = fetch_text(url, headers, cache_name)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


def add_check(g, ok, text):
    g.setdefault("check", []).append({"ok": ok, "text": text})


CHECKS = {}


def summary_add(lg, source, verified, problems, note=""):
    CHECKS.setdefault(lg, []).append({"source": source, "verified": verified, "problems": problems, "note": note})


def norm_team(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower().replace("state", "st").replace("university", ""))


def check_nfl():
    import csv, io
    txt = fetch_text("https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv", cache_name="nflverse_games.csv")
    if not txt:
        summary_add("nfl", "nflverse", 0, 0, "unreachable")
        return
    rows = {r["espn"]: r for r in csv.DictReader(io.StringIO(txt)) if r.get("espn")}
    verified = problems = 0
    for gid, f in FIN.get("nfl", {}).items():
        r = rows.get(gid)
        if not r or r.get("away_score") in ("", None):
            continue
        verified += 1
        if (float(r["away_score"]), float(r["home_score"])) != (f["away"], f["home"]):
            problems += 1
            print("NFL score mismatch", gid, r["away_score"], r["home_score"], f, file=sys.stderr)
    lines_checked = 0
    for g in OUT_DATA.get("nfl") or []:
        r = rows.get(g["id"])
        if not r or g.get("state") != "pre":
            continue
        ln = g["lines"]
        try:
            sp, tot = float(r["spread_line"]), float(r["total_line"])
        except (TypeError, ValueError):
            continue
        lines_checked += 1
        ours_home = ln.get("spreadNum")
        if ours_home is not None:
            d = abs(ours_home - (-sp))
            add_check(g, d < 1.5, f"Spread: DraftKings {fsp(ours_home)} for {g['home']['abbr']} vs nflverse consensus {fsp(-sp)}" + ("" if d < 1.5 else " — differs, check the latest line"))
        if ln.get("totalNum") is not None:
            d = abs(ln["totalNum"] - tot)
            add_check(g, d < 2, f"Total: DraftKings {ln['totalNum']:g} vs nflverse consensus {tot:g}" + ("" if d < 2 else " — differs, check the latest line"))
    # player game logs behind the props
    ptxt = fetch_text("https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_2026.csv", cache_name="nflverse_pw.csv")
    pchecked = pbad = 0
    if ptxt:
        logs = {}
        for r in csv.DictReader(io.StringIO(ptxt)):
            if r.get("season_type") != "REG":
                continue
            logs.setdefault((norm(r["player_display_name"]), r["team"]), []).append(r)
        col = {"Passing": ["passing_yards"], "Rushing": ["rushing_yards"], "Receiving": ["receiving_yards"], "Receptions": ["receptions"],
               "Rush + Rec": ["rushing_yards", "receiving_yards"], "Touchdowns": ["rushing_tds", "receiving_tds"]}
        team_alias = {"LAR": "LA", "WSH": "WAS"}
        for g in OUT_DATA.get("nfl") or []:
            for p in g.get("props") or []:
                if p["cat"] not in col or p.get("line") is None:
                    continue
                L = logs.get((norm(p.get("full") or p["player"]), team_alias.get(p["team"], p["team"])))
                if not L:
                    continue
                vals = [sum(float(r[c] or 0) for c in col[p["cat"]]) for r in sorted(L, key=lambda r: int(r["week"]))][-len(p["l5"]):][::-1]
                pchecked += 1
                # Small differences come from scoring corrections (e.g. a reception reclassified as a run).
                off = [abs(a - b) for a, b in zip(p["l5"], vals)]
                tol = 1 if p["cat"] in ("Receptions", "Touchdowns") else 3
                if len(vals) != len(p["l5"]) or max(off or [0]) > tol:
                    pbad += 1
                    p["check"] = {"ok": False, "text": f"nflverse last {len(vals)}: {', '.join(f'{v:g}' for v in vals)}"}
                else:
                    p["check"] = {"ok": True, "text": "Last-5 numbers match nflverse"}
    summary_add("nfl", "nflverse", verified, problems, f"final scores · {lines_checked} lines compared · {pchecked} prop game logs checked, {pbad} differ")


def check_cfbd():
    key = (os.environ.get("CFBD_API_KEY") or "").strip() or None
    if not key:
        summary_add("cfb", "CollegeFootballData", 0, 0, "no API key in this session")
        return
    H = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    year = datetime.now(ET).year if datetime.now(ET).month >= 6 else datetime.now(ET).year - 1
    games = as_list(fetch_json(f"https://api.collegefootballdata.com/games?year={year}&seasonType=regular", H, f"cfbd_games_{year}.json"), "api.collegefootballdata.com")
    by_ids = {}
    for x in games:
        h, a = x.get("homeTeam") or x.get("home_team"), x.get("awayTeam") or x.get("away_team")
        by_ids[(norm_team(a), norm_team(h), (x.get("startDate") or x.get("start_date") or "")[:10])] = x
    verified = problems = 0
    # Match finals by team names and date; ESPN and CFBD names differ slightly, so compare loosely.
    names = {}
    for g in OUT_DATA.get("cfb") or []:
        for s in ("away", "home"):
            names[g[s]["id"]] = norm_team(g[s].get("loc") or g[s]["name"])
    cf = {}
    for x in games:
        hp, ap = x.get("homePoints", x.get("home_points")), x.get("awayPoints", x.get("away_points"))
        if hp is None or ap is None:
            continue
        cf.setdefault(norm_team(x.get("homeTeam") or x.get("home_team")), []).append((norm_team(x.get("awayTeam") or x.get("away_team")), ap, hp))
    for g in OUT_DATA.get("cfb") or []:
        if g.get("state") != "post" or g["id"] not in FIN["cfb"]:
            continue
        f = FIN["cfb"][g["id"]]
        cand = [c for c in cf.get(names[g["home"]["id"]], []) if c[0] == names[g["away"]["id"]]]
        if not cand:
            continue
        verified += 1
        if (float(cand[-1][1]), float(cand[-1][2])) != (f["away"], f["home"]):
            problems += 1
            add_check(g, False, f"Final score differs from CollegeFootballData ({cand[-1][1]}-{cand[-1][2]})")
    week = (OUT_DATA.get("cfb_meta") or {}).get("week")
    lines = as_list(fetch_json(f"https://api.collegefootballdata.com/lines?year={year}&week={week}&seasonType=regular", H), "api.collegefootballdata.com")
    lc = 0
    for x in lines:
        h, a = norm_team(x.get("homeTeam") or x.get("home_team")), norm_team(x.get("awayTeam") or x.get("away_team"))
        g = next((g for g in OUT_DATA.get("cfb") or [] if names.get(g["home"]["id"]) == h and names.get(g["away"]["id"]) == a), None)
        if not g or g.get("state") != "pre":
            continue
        sps = [pnum(l.get("spread")) for l in x.get("lines") or [] if pnum(l.get("spread")) is not None]
        tts = [pnum(l.get("overUnder") or l.get("over_under")) for l in x.get("lines") or [] if pnum(l.get("overUnder") or l.get("over_under")) is not None]
        books = ", ".join(sorted({l.get("provider", "") for l in x.get("lines") or [] if l.get("provider")}))
        if sps and g["lines"].get("spreadNum") is not None:
            med = sorted(sps)[len(sps) // 2]
            d = abs(med - g["lines"]["spreadNum"])
            add_check(g, d < 1.5, f"Spread: DraftKings {fsp(g['lines']['spreadNum'])} vs {len(sps)} books ({books}) median {fsp(med)}" + ("" if d < 1.5 else " — differs"))
            lc += 1
        if tts and g["lines"].get("totalNum") is not None:
            med = sorted(tts)[len(tts) // 2]
            d = abs(med - g["lines"]["totalNum"])
            add_check(g, d < 2, f"Total: DraftKings {g['lines']['totalNum']:g} vs median {med:g}" + ("" if d < 2 else " — differs"))
    err = LAST_ERR.get("api.collegefootballdata.com")
    sample = next((f"{x.get('awayTeam') or x.get('away_team')} @ {x.get('homeTeam') or x.get('home_team')}" for x in games), "none")
    summary_add("cfb", "CollegeFootballData", verified, problems,
                f"final scores · {lc} lines compared across books · {len(games)} games and {len(lines)} line sets received"
                + (f" · error: {err}" if err else f" · e.g. {sample}"))
    # stadium coordinates for the weather check
    venues = fetch_json("https://api.collegefootballdata.com/venues", H, "cfbd_venues.json") or []
    return {norm_team(v.get("name")): (v.get("location") or {}) for v in venues}


def check_cbbd():
    key = ((os.environ.get("CFBD_API_KEY") or os.environ.get("CBBD_API_KEY") or "").strip()) or None
    if not key:
        summary_add("cbb", "CollegeBasketballData", 0, 0, "no API key in this session")
        return
    H = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    now = datetime.now(ET)
    season = now.year + 1 if now.month >= 8 else now.year
    games = as_list(fetch_json(f"https://api.collegebasketballdata.com/games?season={season}", H), "api.collegebasketballdata.com")
    done = [x for x in games if x.get("homePoints") is not None and x.get("awayPoints") is not None]
    names = {}
    for g in OUT_DATA.get("cbb") or []:
        for s in ("away", "home"):
            names[g[s]["id"]] = norm_team(g[s].get("loc") or g[s]["name"])
    verified = problems = 0
    idx = {(norm_team(x.get("awayTeam")), norm_team(x.get("homeTeam"))): x for x in done}
    for g in OUT_DATA.get("cbb") or []:
        if g.get("state") != "post" or g["id"] not in FIN.get("cbb", {}):
            continue
        x = idx.get((names[g["away"]["id"]], names[g["home"]["id"]]))
        if not x:
            continue
        verified += 1
        f = FIN["cbb"][g["id"]]
        if (float(x["awayPoints"]), float(x["homePoints"])) != (f["away"], f["home"]):
            problems += 1
            add_check(g, False, f"Final score differs from CollegeBasketballData ({x['awayPoints']}-{x['homePoints']})")
    err = LAST_ERR.get("api.collegebasketballdata.com")
    summary_add("cbb", "CollegeBasketballData", verified, problems, f"{len(done)} completed games in their feed" + (f" · error: {err}" if err else ""))


ODDS_SPORT = {"nfl": "americanfootball_nfl", "nba": "basketball_nba", "cbb": "basketball_ncaab"}
FILL_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "betrivers", "espnbet", "fanatics", "bovada"]


def fill_lines(lg, g, e, h):
    """ESPN has no DraftKings line yet (books post at different times): take the game's lines from the first US book
    The Odds API lists, in FILL_BOOKS order, and score the game from them. The book is named on the board."""
    books = {bk.get("key"): bk for bk in e.get("bookmakers") or []}
    bk = next((books[k] for k in FILL_BOOKS if k in books), None) or next(iter(books.values()), None)
    if not bk:
        return
    ln = g["lines"]
    for mk in bk.get("markets") or []:
        for oc in mk.get("outcomes") or []:
            home = norm_team(oc.get("name")) == h
            if mk["key"] == "spreads" and oc.get("point") is not None and ln.get("spreadNum") is None:
                if home:
                    ln["spreadNum"] = oc["point"]; ln["homeSpread"] = fmt_spread(oc["point"]); ln["awaySpread"] = fmt_spread(-oc["point"])
                ln["homeSpreadOdds" if home else "awaySpreadOdds"] = fmt_ml(oc.get("price"))
            elif mk["key"] == "totals" and oc.get("point") is not None and (ln.get("totalNum") is None or ln.get("filled")):
                ln["totalNum"] = oc["point"]; ln["total"] = f"{oc['point']:g}"
                ln["overOdds" if oc.get("name") == "Over" else "underOdds"] = fmt_ml(oc.get("price"))
            elif mk["key"] == "h2h" and (ln.get("homeML") in (None, "—") or ln.get("filled")):
                ln["homeML" if home else "awayML"] = fmt_ml(oc.get("price"))
    ln["book"] = bk.get("title")
    ln["filled"] = True


def value_conf_bb(lg, g):
    """Price-model confidence for a basketball game whose lines arrived after its slate was built (same math as the
    basketball slate: edge against the line, weighted by how much the model is trusted this early)."""
    m, ln = g.get("model"), g["lines"]
    if not m:
        return
    unit = 0.45 if lg == "nba" else 0.6
    w, pa, ph = m.get("trust", 0.2), m["away"], m["home"]
    margin = ph - pa
    tot_proj = pa + ph
    if lg == "cbb" and CBB_VALUE_FIT and m.get("src") != "torvik":
        # Nov 9: early-season college ratings squeeze margins (lines of -19 read as -8), so every read leaned underdog.
        # The margin is put on the market's scale for the day's slate; what's left is real disagreement with the line.
        a_, b_ = CBB_VALUE_FIT
        margin = a_ + b_ * margin
    tv_ = g.get("torvik") if lg == "cbb" else None
    if tv_:
        # Torvik as a tool (Nov 9): his ratings carry most of the margin and total early in the season, ours more later.
        wt = 0.7 if g.get("early") is not None else 0.5
        margin = wt * tv_["margin"] + (1 - wt) * margin
        tot_proj = wt * tv_["total"] + (1 - wt) * tot_proj
        w = max(w, 0.6)
    if lg == "cbb":
        m["pHome"] = round(phi(margin / 11.0), 3)
    cs = g.get("confidence") or {"moneyline": [0.0, "Pass"], "spread": [0.0, "Pass"], "total": [0.0, "Pass"]}
    spread, total = ln.get("spreadNum"), ln.get("totalNum")
    if spread is not None:
        edge = w * (margin + spread)
        sc = conf(abs(edge) / unit)
        side = f"{g['home']['abbr']} {fmt_spread(spread)}" if edge > 0 else f"{g['away']['abbr']} {fmt_spread(-spread)}"
        cs["spread"] = [sc, label(sc, side)]; m.update(spreadEdge=round(abs(edge), 1), spreadSide=side)
    if total is not None:
        edge = w * (tot_proj - total)
        sc = conf(abs(edge) / (unit * 1.5))
        side = ("Over " if edge > 0 else "Under ") + f"{total:g}"
        cs["total"] = [sc, label(sc, side)]; m.update(totalEdge=round(abs(edge), 1), totalSide=side)
    aml, hml = pnum(ln.get("awayML")), pnum(ln.get("homeML"))
    da, dh = american(aml), american(hml)
    if da and dh:
        pm = (1 / dh) / (1 / dh + 1 / da)
        pb = w * m["pHome"] + (1 - w) * pm
        ev_h, ev_a = pb * dh - 1, (1 - pb) * da - 1
        best, ml, ev = (g["home"], hml, ev_h) if ev_h >= ev_a else (g["away"], aml, ev_a)
        sc = conf(max(0, ev) * 100 / 5)
        if (ml or 0) > 500:
            sc = min(sc, 5.0)
        cs["moneyline"] = [sc, label(sc, f"{best['abbr']} {fmt_ml(ml)}")]
        m.update(mlEV=round(ev, 3), mlSide=f"{best['abbr']} {fmt_ml(ml)}")
    g["confidence"] = cs


def check_odds():
    key = (os.environ.get("ODDS_API_KEY") or "").strip() or None
    if not key:
        for lg in ODDS_SPORT:
            summary_add(lg, "The Odds API", 0, 0, "no API key in this session")
        return
    # Credits are limited: call only to fill games missing lines, plus the morning cross-check of DraftKings.
    morning = datetime.now(ET).hour in (8, 9, 10)
    for lg, sport in ODDS_SPORT.items():
        slate = [g for g in OUT_DATA.get(lg) or [] if g.get("state") == "pre"]
        missing = any(g["lines"].get("spreadNum") is None for g in slate)
        if not slate or not (missing or morning):
            if slate:
                summary_add(lg, "The Odds API", 0, 0, "skipped to save API credits (no missing lines; cross-check runs in the morning)")
            continue
        evs = fetch_json(f"https://api.the-odds-api.com/v4/sports/{sport}/odds?regions=us&markets=h2h,spreads,totals&oddsFormat=american&apiKey={key}") or []
        compared = 0
        for e in evs:
            h, a = norm_team(e.get("home_team")), norm_team(e.get("away_team"))
            g = next((g for g in slate if norm_team(g["home"].get("full")) == h and norm_team(g["away"].get("full")) == a), None)
            if not g:
                continue
            if g["lines"].get("spreadNum") is None or g["lines"].get("totalNum") is None:
                fill_lines(lg, g, e, h)
            sps, tts, best = [], [], {}
            for bk in e.get("bookmakers") or []:
                for mk in bk.get("markets") or []:
                    for oc in mk.get("outcomes") or []:
                        if mk["key"] == "spreads" and norm_team(oc["name"]) == h and oc.get("point") is not None:
                            sps.append(oc["point"])
                        if mk["key"] == "totals" and oc.get("name") == "Over" and oc.get("point") is not None:
                            tts.append(oc["point"])
                        k2 = (mk["key"], norm_team(oc["name"]) if mk["key"] == "spreads" else oc["name"])
                        if oc.get("price") is not None and (k2 not in best or oc["price"] > best[k2][0]):
                            best[k2] = (oc["price"], bk.get("title"), oc.get("point"))
            if sps and g["lines"].get("spreadNum") is not None:
                med = sorted(sps)[len(sps) // 2]
                d = abs(med - g["lines"]["spreadNum"])
                add_check(g, d < 1.5, f"Spread: DraftKings {fsp(g['lines']['spreadNum'])} vs {len(sps)} books median {fsp(med)}" + ("" if d < 1.5 else " — differs"))
            if tts and g["lines"].get("totalNum") is not None:
                med = sorted(tts)[len(tts) // 2]
                d = abs(med - g["lines"]["totalNum"])
                add_check(g, d < 2, f"Total: DraftKings {g['lines']['totalNum']:g} vs {len(tts)} books median {med:g}" + ("" if d < 2 else " — differs"))
            g["best"] = {f"{k[0]}:{k[1]}": {"price": fmt_ml(v[0]), "book": v[1], "point": v[2]} for k, v in best.items()}
            compared += 1
        summary_add(lg, "The Odds API", compared, 0, "lines compared across US books")
    sc = (fetch_json(f"https://api.the-odds-api.com/v4/sports/basketball_nba/scores?daysFrom=3&apiKey={key}") or []) if morning else []
    verified = problems = 0
    for e in sc:
        if not e.get("completed") or not e.get("scores"):
            continue
        pts = {norm_team(s["name"]): float(s["score"]) for s in e["scores"]}
        for g in OUT_DATA.get("nba") or []:
            if g["id"] in FIN.get("nba", {}) and norm_team(g["home"].get("full")) in pts and norm_team(g["away"].get("full")) in pts:
                f = FIN["nba"][g["id"]]
                verified += 1
                if (pts[norm_team(g["away"]["full"])], pts[norm_team(g["home"]["full"])]) != (f["away"], f["home"]):
                    problems += 1
                    add_check(g, False, "Final score differs from The Odds API")
    summary_add("nba", "The Odds API scores", verified, problems, "last 3 days of final scores")


# Outdoor NFL stadiums (latitude, longitude) for the weather check; domes and roofs are skipped.
NFL_STADIUMS = {"BUF": (42.774, -78.787), "MIA": (25.958, -80.239), "NE": (42.091, -71.264), "NYJ": (40.814, -74.074), "NYG": (40.814, -74.074),
                "BAL": (39.278, -76.623), "CIN": (39.095, -84.516), "CLE": (41.506, -81.699), "PIT": (40.447, -80.016), "JAX": (30.324, -81.637),
                "TEN": (36.166, -86.771), "KC": (39.049, -94.484), "DEN": (39.744, -105.020), "LAC": None, "LAR": None, "PHI": (39.901, -75.168),
                "WSH": (38.908, -76.864), "CHI": (41.862, -87.617), "GB": (44.501, -88.062), "CAR": (35.226, -80.853), "TB": (27.976, -82.503),
                "SF": (37.403, -121.970), "SEA": (47.595, -122.332)}


def nws_forecast(lat, lon, when_iso):
    pt = fetch_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", cache_name=f"nws_pt_{lat:.2f}_{lon:.2f}.json")
    url = ((pt or {}).get("properties") or {}).get("forecastHourly")
    if not url:
        return None
    fc = fetch_json(url)
    t = datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
    for per in ((fc or {}).get("properties") or {}).get("periods") or []:
        a = datetime.fromisoformat(per["startTime"]); b = datetime.fromisoformat(per["endTime"])
        if a <= t < b:
            wind = max((int(x) for x in re.findall(r"\d+", per.get("windSpeed") or "0")), default=0)
            rain = ((per.get("probabilityOfPrecipitation") or {}).get("value")) or 0
            return {"temp": per.get("temperature"), "rain": int(rain), "wind": wind, "short": per.get("shortForecast")}
    return None


def check_weather(cfb_venues):
    n = upd = 0
    for lg in ("nfl", "cfb"):
        for g in OUT_DATA.get(lg) or []:
            if g.get("state") != "pre" or "Indoors" in str(g.get("weather")):
                continue
            iso = g.get("iso")
            if not iso:
                continue
            if lg == "nfl":
                ll = NFL_STADIUMS.get(g["home"]["abbr"])
            else:
                loc = (cfb_venues or {}).get(norm_team(g.get("stadium")))
                ll = (loc.get("y") or loc.get("latitude"), loc.get("x") or loc.get("longitude")) if loc else None
            if not ll or ll[0] is None:
                continue
            fc = nws_forecast(float(ll[0]), float(ll[1]), iso)
            if not fc:
                continue
            n += 1
            g["wx"] = {"indoor": False, "rain": fc["rain"], "wind": fc["wind"]}
            espn = weather_of(g)
            same = abs(espn["rain"] - fc["rain"]) < 25 and abs(espn["wind"] - fc["wind"]) < 10
            add_check(g, same, f"Weather.gov at kickoff: {fc['temp']}°, {fc['rain']}% rain, {fc['wind']} mph wind ({fc['short']})"
                      + ("" if same else " — differs from ESPN's forecast; warnings use weather.gov"))
            upd += 0 if same else 1
    summary_add("nfl", "weather.gov", n, upd, "kickoff forecasts for outdoor games")


def run_checks():
    try:
        check_nfl()
    except Exception as e:
        print("nfl check failed", safe_error(e), file=sys.stderr); summary_add("nfl", "nflverse", 0, 0, "check failed")
    venues = None
    try:
        venues = check_cfbd()
    except Exception as e:
        print("cfbd check failed", safe_error(e), file=sys.stderr); summary_add("cfb", "CollegeFootballData", 0, 0, "check failed")
    try:
        check_cbbd()
    except Exception as e:
        print("cbbd check failed", safe_error(e), file=sys.stderr); summary_add("cbb", "CollegeBasketballData", 0, 0, "check failed")
    try:
        check_odds()
    except Exception as e:
        print("odds check failed", safe_error(e), file=sys.stderr)
    try:
        check_weather(venues)
    except Exception as e:
        print("weather check failed", safe_error(e), file=sys.stderr)
    OUT_DATA["checks"] = CHECKS


run_checks()
attach_analysis()
update_history(sys.argv[3] if len(sys.argv) > 3 else None, sys.argv[4] if len(sys.argv) > 4 else None)
build_recap()

OUT_DATA["logs"] = PLAYER_LOGS  # per-player game logs behind the prop tables (fields: LOG_FIELDS)
OUT_DATA["logFields"] = LOG_FIELDS
with open(OUT, "w") as f:
    json.dump(OUT_DATA, f, separators=(",", ":"))
print("wrote", OUT, os.path.getsize(OUT), file=sys.stderr)
