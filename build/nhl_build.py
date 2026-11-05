"""NHL board (JD-style hockey sheets): python3 -I build/nhl_build.py <cache_dir> <out_json> [YYYYMMDD]

Team stats from every completed game this season (ESPN box scores), today's slate with probable goalies and
DraftKings lines, and confidence levels for Moneyline, Puck line and Total on the board's scale
(Pass under 5, Lean 5 to 6.9, Play 7 and up).
"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
B = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
CACHE, OUT = sys.argv[1], sys.argv[2]
DAY = sys.argv[3] if len(sys.argv) > 3 else datetime.now(ET).strftime("%Y%m%d")
SEASON_START = "20261006"
os.makedirs(CACHE, exist_ok=True)


def get(url, name=None):
    path = os.path.join(CACHE, name) if name else None
    if path and os.path.exists(path):
        return json.load(open(path))
    for i in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.load(r)
            if path:
                json.dump(d, open(path, "w"))
            return d
        except Exception:
            time.sleep(2 ** i)
    return None


def num(x):
    try:
        return float(str(x).replace("%", ""))
    except (TypeError, ValueError):
        return None


# ---------- completed games ----------
start = datetime.strptime(SEASON_START, "%Y%m%d")
today = datetime.strptime(DAY, "%Y%m%d")
done = []
d = start
while d < today:
    ds = d.strftime("%Y%m%d")
    sb = get(f"{B}/scoreboard?dates={ds}", f"nhl_sb_{ds}.json")
    for e in (sb or {}).get("events", []):
        if e["status"]["type"].get("completed"):
            done.append(e["id"])
    d += timedelta(days=1)


def parse(eid):
    s = get(f"{B}/summary?event={eid}", f"nhl_sum_{eid}.json")
    if not s:
        return None
    comp = s["header"]["competitions"][0]
    T = {}
    for c in comp["competitors"]:
        T[c["team"]["id"]] = {"id": c["team"]["id"], "abbr": c["team"]["abbreviation"], "ha": c["homeAway"],
                              "g": num(c.get("score")) or 0, "win": bool(c.get("winner"))}
    for t in s.get("boxscore", {}).get("teams", []):
        r = T.get(t["team"]["id"])
        st = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        r.update(sog=num(st.get("shotsTotal")), hits=num(st.get("hits")), blocks=num(st.get("blockedShots")),
                 ppg=num(st.get("powerPlayGoals")) or 0, ppo=num(st.get("powerPlayOpportunities")) or 0)
    for t in s.get("boxscore", {}).get("players", []):
        r = T.get(t["team"]["id"])
        sog = 0.0
        for cat in t.get("statistics", []):
            lab = cat.get("labels") or []
            for a in cat.get("athletes", []):
                v = dict(zip(lab, a.get("stats", [])))
                if cat.get("name") == "goalies":
                    toi = v.get("TOI") or "0:00"
                    mins = int(toi.split(":")[0]) if ":" in toi else 0
                    if mins >= 30:
                        r["goalie"] = {"id": a["athlete"]["id"], "name": a["athlete"].get("displayName"),
                                       "ga": num(v.get("GA")) or 0, "sa": num(v.get("SA")) or 0, "sv": num(v.get("SV")) or 0}
                else:
                    sog += (num(v.get("S")) or 0) + (num(v.get("SM")) or 0)  # attempts that reached the net or missed it
        r["att"] = sog
    if len(T) != 2:
        return None
    return {"id": comp["id"], "date": comp["date"], "teams": T}


with ThreadPoolExecutor(16) as ex:
    games = sorted([g for g in ex.map(parse, done) if g], key=lambda g: g["date"])
print("completed", len(games), file=sys.stderr)

per = {}
for g in games:
    for tid in g["teams"]:
        per.setdefault(tid, []).append(g)


def window(gl, tid):
    """Per-game averages for a team over these games (offense, defense, goalie side)."""
    if not gl:
        return {}
    o = lambda g: g["teams"][tid]
    p = lambda g: next(v for k, v in g["teams"].items() if k != tid)
    n = len(gl)
    avg = lambda f: sum(f(g) or 0 for g in gl) / n
    ppo, ppg = sum(o(g)["ppo"] for g in gl), sum(o(g)["ppg"] for g in gl)
    pko, pkg = sum(p(g)["ppo"] for g in gl), sum(p(g)["ppg"] for g in gl)
    sa = sum(o(g).get("goalie", {}).get("sa", 0) for g in gl) or sum(p(g)["sog"] for g in gl)
    sv = sum(o(g).get("goalie", {}).get("sv", 0) for g in gl)
    return {"gf": avg(lambda g: o(g)["g"]), "shots": avg(lambda g: o(g).get("att") or 0), "sog": avg(lambda g: o(g)["sog"]),
            "hits": avg(lambda g: o(g)["hits"]), "pp": 100 * ppg / ppo if ppo else None, "blocks": avg(lambda g: o(g)["blocks"]),
            "ga": avg(lambda g: p(g)["g"]), "o_shots": avg(lambda g: p(g).get("att") or 0), "o_sog": avg(lambda g: p(g)["sog"]),
            "o_hits": avg(lambda g: p(g)["hits"]), "pk": 100 * (1 - pkg / pko) if pko else None, "o_blocks": avg(lambda g: p(g)["blocks"]),
            "svpct": sv / sa if sa else None, "win": 100 * avg(lambda g: 1.0 if o(g)["win"] else 0.0), "n": n}


agg = {t: {"season": window(gl, t), "l10": window(gl[-10:], t), "l5": window(gl[-5:], t),
           "home": window([g for g in gl if g["teams"][t]["ha"] == "home"], t),
           "away": window([g for g in gl if g["teams"][t]["ha"] == "away"], t)} for t, gl in per.items()}

# rows: (section, label, key, higher_is_better)
ROWS = [("Offense", "Goals/G", "gf", True), ("Offense", "Shots/G", "shots", True), ("Offense", "SOG/G", "sog", True),
        ("Offense", "Hits/G", "hits", True), ("Offense", "PP%", "pp", True), ("Offense", "Blocks/G", "blocks", True),
        ("Defense", "Opp Goals/G", "ga", False), ("Defense", "Opp Shots/G", "o_shots", False), ("Defense", "Opp SOG/G", "o_sog", False),
        ("Defense", "Opp Hits/G", "o_hits", False), ("Defense", "PK%", "pk", True), ("Defense", "Opp Blocks/G", "o_blocks", False)]
NEUTRAL = {"hits", "o_hits", "blocks", "o_blocks"}  # style stats: shown and ranked, not counted as better or worse


def rank_map(vals, higher):
    xs = sorted([(v, t) for t, v in vals.items() if v is not None], reverse=higher)
    return {t: i + 1 for i, (v, t) in enumerate(xs)}


ranks = {k: rank_map({t: a["l5"].get(k) for t, a in agg.items()}, hb) for _, _, k, hb in ROWS}
NT = len(agg)
LG_GF = sum(a["season"]["gf"] * a["season"]["n"] for a in agg.values()) / max(1, sum(a["season"]["n"] for a in agg.values()))

# goalies: every start this season
gstats = {}
for g in games:
    for tid, t in g["teams"].items():
        gk = t.get("goalie")
        if not gk:
            continue
        s = gstats.setdefault(gk["id"], {"name": gk["name"], "gp": 0, "ga": 0, "sa": 0, "sv": 0, "w": 0, "log": []})
        s["gp"] += 1; s["ga"] += gk["ga"]; s["sa"] += gk["sa"]; s["sv"] += gk["sv"]; s["w"] += t["win"]
        s["log"].append({**gk, "ha": t["ha"], "win": t["win"]})


def gwin(log):
    """Goalie split: goals allowed, saves and shots against per start, save %, win %."""
    if not log:
        return None
    n, sa, sv = len(log), sum(x["sa"] for x in log), sum(x["sv"] for x in log)
    return {"ga": sum(x["ga"] for x in log) / n, "sv": sv / n, "svp": 100 * sv / sa if sa else None, "sa": sa / n,
            "win": 100 * sum(1 for x in log if x["win"]) / n}


G_ROWS = [("Goals Allowed", "ga", False), ("Saves", "sv", True), ("Save%", "svp", True), ("Shots Against", "sa", False), ("Win%", "win", True)]
G_L5 = {gid: gwin(st["log"][-5:]) for gid, st in gstats.items() if st["gp"] >= 2}
G_RANK = {k: rank_map({gid: w[k] for gid, w in G_L5.items() if w}, hb) for _, k, hb in G_ROWS}


def gsheet(gid):
    st = gstats.get(gid)
    if not st:
        return None
    lg = st["log"]
    return {"season": gwin(lg), "away": gwin([x for x in lg if x["ha"] == "away"]), "home": gwin([x for x in lg if x["ha"] == "home"]),
            "l10": gwin(lg[-10:]), "l5": gwin(lg[-5:]), "rank": {k: G_RANK[k].get(gid) for _, k, _ in G_ROWS}}


def gline(gid):
    s = gstats.get(gid)
    if not s or not s["gp"]:
        return None
    l5 = s["log"][-5:]
    sa5, sv5 = sum(x["sa"] for x in l5), sum(x["sv"] for x in l5)
    return {"name": s["name"], "gp": s["gp"], "w": s["w"], "gaa": s["ga"] / s["gp"], "svpct": s["sv"] / s["sa"] if s["sa"] else None,
            "ga5": sum(x["ga"] for x in l5) / len(l5), "sv5": sv5 / sa5 if sa5 else None, "saves5": sv5 / len(l5), "sa5": sa5 / len(l5)}


gl_all = [x for x in (gline(i) for i in gstats) if x and x["gp"] >= 3 and x["svpct"]]
SV_RANK = {round(x["svpct"], 4): i + 1 for i, x in enumerate(sorted(gl_all, key=lambda x: -x["svpct"]))}
LG_SV = sum(x["svpct"] for x in gl_all) / len(gl_all) if gl_all else 0.9


def american(p):
    p = min(0.99, max(0.01, p))
    return f"-{round(100 * p / (1 - p))}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def label(sc, call):
    return f"Play {call}" if sc >= 7 else f"Lean {call}" if sc >= 5 else "Pass"


def to_scale(raw):
    """Board scale: raw 0-10 strength to Pass under 5 / Lean 5-6.9 / Play 7+ (raw 4 is the Lean line)."""
    return round(raw if raw >= 7 else 5 + (raw - 4) * 2 / 3 if raw >= 4 else 4 * raw / 4 * 1.2 if raw < 4 else raw, 1)


# ---------- today's slate ----------
sb = get(f"{B}/scoreboard?dates={DAY}")
slate = []
for e in (sb or {}).get("events", []):
    c = e["competitions"][0]
    side = {x["homeAway"]: x for x in c["competitors"]}
    A, H = side["away"], side["home"]
    aid, hid = A["team"]["id"], H["team"]["id"]
    if aid not in agg or hid not in agg:
        continue
    o = (c.get("odds") or [{}])[0]
    ml = o.get("moneyline") or {}
    tot = o.get("total") or {}
    ps = o.get("pointSpread") or {}
    pick_ = lambda d, k: ((d.get(k) or {}).get("close") or (d.get(k) or {}).get("open") or {})
    lines = {"awayML": pick_(ml, "away").get("odds"), "homeML": pick_(ml, "home").get("odds"),
             "total": num(pick_(tot, "over").get("line", "").lstrip("ou")) if pick_(tot, "over").get("line") else o.get("overUnder"),
             "overOdds": pick_(tot, "over").get("odds"), "underOdds": pick_(tot, "under").get("odds"),
             "awayPL": pick_(ps, "away").get("line"), "awayPLOdds": pick_(ps, "away").get("odds"),
             "homePL": pick_(ps, "home").get("line"), "homePLOdds": pick_(ps, "home").get("odds"), "details": o.get("details")}
    if lines["total"] is None:
        lines["total"] = o.get("overUnder")
    goalies = {}
    for sd, x in (("away", A), ("home", H)):
        pr = (x.get("probables") or [{}])[0]
        gid = (pr.get("athlete") or {}).get("id")
        gl_ = gline(gid) if gid else None
        goalies[sd] = {"name": (pr.get("athlete") or {}).get("displayName"), "status": (pr.get("status") or {}).get("name") or "probable",
                       "sheet": gsheet(gid) if gid else None,
                       **(gl_ or {}), "svRank": SV_RANK.get(round(gl_["svpct"], 4)) if gl_ and gl_.get("svpct") else None}
    ta, th = agg[aid], agg[hid]
    sections = {}
    for sec, lab, k, hb in ROWS:
        sections.setdefault(sec, []).append([lab, [ta[w].get(k) for w in ("season", "away", "l10", "l5")] + [ranks[k].get(aid)],
                                             [ranks[k].get(hid)] + [th[w].get(k) for w in ("l5", "l10", "home", "season")], k])

    # ---- matchup strength (> 0 favors away): unit matchups + categories won + goalie edge ----
    def rk(k, t):
        return ranks[k].get(t) or NT / 2
    pairs = [("gf", "ga", 2.0), ("sog", "o_sog", 1.0), ("pp", "pk", 1.0), ("shots", "o_shots", 0.5)]
    unit = 0.0
    notes = []
    for off, dfn, w in pairs:
        a_mm = (rk(dfn, hid) - rk(off, aid)) / NT  # away offense vs home defense (> 0 = away offense out-ranks it)
        h_mm = (rk(dfn, aid) - rk(off, hid)) / NT
        unit += w * (a_mm - h_mm)
    better_a = sum(1 for _, _, k, hb in ROWS if k not in NEUTRAL and rk(k, aid) < rk(k, hid))
    better_h = sum(1 for _, _, k, hb in ROWS if k not in NEUTRAL and rk(k, hid) < rk(k, aid))
    share = (better_a - better_h) / max(1, better_a + better_h)
    ga, gh = goalies["away"], goalies["home"]
    g_edge = 0.0
    if ga.get("svpct") and gh.get("svpct"):
        g_edge = ((0.6 * (ga.get("sv5") or ga["svpct"]) + 0.4 * ga["svpct"]) - (0.6 * (gh.get("sv5") or gh["svpct"]) + 0.4 * gh["svpct"])) / 0.01  # points of save %
    strength = 1.6 * unit + 1.5 * share + 0.6 * g_edge
    ml_raw = min(10.0, abs(strength) * 0.9)
    fav = "away" if strength > 0 else "home"
    ml_price = num(lines["awayML"] if fav == "away" else lines["homeML"])
    ml_call = f"{(A if fav == 'away' else H)['team']['abbreviation']} {lines['awayML'] if fav == 'away' else lines['homeML'] or 'ML'}"
    if ml_price is not None and ml_price <= -250:
        ml_raw = min(ml_raw, 6.9)  # heavy favorite: never a bet on the moneyline
    # puck line: the better team has to win by 2; a coin-flip matchup can't get there
    pl_raw = max(0.0, ml_raw - 3.0)
    pl_call = f"{(A if fav == 'away' else H)['team']['abbreviation']} -1.5"
    # ---- total, JD's way: three last-5 sums against the line -- the two goalies' goals allowed, the two offenses'
    # goals scored, and the two defenses' goals allowed (Nov 5 video: "goalies giving up 4.6, teams giving up 5.2,
    # scoring 5.8 against 5.5"). The projection is their average; about one goal of gap reads as a 9.
    g_sum = (ga.get("ga5") or ta["l5"]["ga"]) + (gh.get("ga5") or th["l5"]["ga"])
    o_sum = ta["l5"]["gf"] + th["l5"]["gf"]
    d_sum = ta["l5"]["ga"] + th["l5"]["ga"]
    proj = (g_sum + o_sum + d_sum) / 3
    tl = num(lines["total"])
    tot_raw, tot_call = 0.0, "Pass"
    if tl is not None:
        gap = proj - tl
        agree = sum(1 for v in (g_sum, o_sum, d_sum) if (v - tl) * gap > 0)
        tot_raw = min(10.0, abs(gap) * 9.0) * (1.0 if agree == 3 else 0.8 if agree == 2 else 0.5)
        if agree < 3:
            tot_raw = min(tot_raw, 6.9)  # JD passes when one sum disagrees (VAN-WPG Nov 5: goalies say over, offenses don't)
        tot_call = f"{'Over' if gap > 0 else 'Under'} {tl:g}"
    tot_parts = {"goalies": round(g_sum, 2), "offenses": round(o_sum, 2), "defenses": round(d_sum, 2)}
    # notes for the case
    def rname(k):
        return next(l for _, l, kk, _ in ROWS if kk == k)
    for off, dfn, _ in pairs[:3]:
        for o_t, d_t, O, D in ((aid, hid, A, H), (hid, aid, H, A)):
            gap_ = rk(dfn, d_t) - rk(off, o_t)
            if gap_ >= NT * 0.4:
                notes.append(f"{O['team']['abbreviation']} {rname(off)} ({rk(off, o_t)}th) vs {D['team']['abbreviation']} {rname(dfn)} ({rk(dfn, d_t)}th)")
    if ga.get("svpct") and gh.get("svpct") and abs(g_edge) >= 1.0:
        bg = ga if g_edge > 0 else gh
        notes.append(f"Goalie edge: {bg['name']} saves {100 * (bg.get('sv5') or bg['svpct']):.1f}% lately")
    when = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET)
    slate.append({
        "id": e["id"], "time": when.strftime("%-I:%M %p"), "date": when.strftime("%b %-d"), "venue": (c.get("venue") or {}).get("fullName"),
        "away": {"abbr": A["team"]["abbreviation"], "name": A["team"].get("shortDisplayName"), "logo": A["team"].get("logo"),
                 "record": (A.get("records") or [{}])[0].get("summary"), "color": "#" + (A["team"].get("color") or "333333")},
        "home": {"abbr": H["team"]["abbreviation"], "name": H["team"].get("shortDisplayName"), "logo": H["team"].get("logo"),
                 "record": (H.get("records") or [{}])[0].get("summary"), "color": "#" + (H["team"].get("color") or "333333")},
        "lines": lines, "goalies": goalies, "sections": sections,
        "conf": {"moneyline": [to_scale(ml_raw), label(to_scale(ml_raw), ml_call)],
                 "puckline": [to_scale(pl_raw), label(to_scale(pl_raw), pl_call)],
                 "total": [to_scale(tot_raw), label(to_scale(tot_raw), tot_call)]},
        "proj": round(proj, 2), "totParts": tot_parts, "notes": notes[:5],
        "last5": {sd: [[("@" if gg["teams"][t]["ha"] == "away" else "") + next(v["abbr"] for k, v in gg["teams"].items() if k != t),
                        f"{int(gg['teams'][t]['g'])}-{int(next(v['g'] for k, v in gg['teams'].items() if k != t))}", gg["teams"][t]["win"]]
                       for gg in per[t][-5:]] for sd, t in (("away", aid), ("home", hid))}})

json.dump({"day": DAY, "built": datetime.now(ET).strftime("%b %-d, %-I:%M %p ET"), "teams": NT, "games": len(games),
           "lgGF": round(LG_GF, 2), "lgSV": round(LG_SV, 4), "slate": slate}, open(OUT, "w"), separators=(",", ":"))
print("slate", len(slate), file=sys.stderr)
