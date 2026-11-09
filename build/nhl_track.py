"""NHL pick log: python3 -I build/nhl_track.py <board_json> <log_json> <page_json>

Logs every Play and Lean on today's hockey board (moneyline, puck line, total), keeps updating a pick until its game
starts, grades finished games from ESPN final scores, and writes the board plus the log for the page. Entries marked
"backtest" were rebuilt from past days with only the stats available before each day (Oct 20 - Nov 8).
"""
import json, os, sys, time, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
B = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
BOARD, LOG, PAGE = sys.argv[1], sys.argv[2], sys.argv[3]


def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 ** i)
    return None


def pnum(x):
    try:
        return float(str(x).replace("+", ""))
    except (TypeError, ValueError):
        return None


def pick_of(g, kind, conf):
    """(side, line, price) for a board call like 'Play NJ -218', 'Lean NJ -1.5' or 'Play Under 6.5'."""
    lab = conf[1]
    call = lab.split(" ", 1)[1]
    ln = g["lines"]
    if kind == "total":
        side, line = call.split()
        return side.lower(), float(line), ln.get("overOdds" if side == "Over" else "underOdds")
    team = call.split()[0]
    sd = "away" if team in (g["away"]["abbr"], "UTAH" if g["away"]["abbr"] == "UTA" else None) else "home"
    if kind == "moneyline":
        return sd, None, ln.get("awayML" if sd == "away" else "homeML")
    return sd, pnum(ln.get("awayPL" if sd == "away" else "homePL")), ln.get("awayPLOdds" if sd == "away" else "homePLOdds")


def grade(e, sc):
    if e["kind"] == "total":
        t = sc["home"] + sc["away"]
        return "P" if t == e["line"] else ("W" if (t > e["line"]) == (e["side"] == "over") else "L")
    m = sc[e["side"]] - sc["home" if e["side"] == "away" else "away"]
    if e["kind"] == "moneyline":
        return "W" if m > 0 else "L"
    v = m + (e["line"] or 0)
    return "P" if v == 0 else ("W" if v > 0 else "L")


board = json.load(open(BOARD))
log = json.load(open(LOG)) if os.path.exists(LOG) else {"picks": []}
by_id = {e["id"]: e for e in log["picks"]}
started = {}


def state(gid):
    if gid not in started:
        s = get(f"{B}/summary?event={gid}") or {}
        c = ((s.get("header") or {}).get("competitions") or [{}])[0]
        st = (c.get("status") or {}).get("type") or {}
        sc = {x["homeAway"]: pnum(x.get("score")) for x in c.get("competitors", [])}
        started[gid] = (st.get("state"), sc)
    return started[gid]


# Today's board: add or refresh picks until the game starts.
for g in board.get("slate", []):
    st, _ = state(g["id"])
    for kind, conf in g["conf"].items():
        pid = f"{g['id']}-{kind}"
        old = by_id.get(pid)
        if old and (old.get("result") or old.get("locked")):
            continue
        if st not in (None, "pre"):
            if old:
                old["locked"] = True
            continue
        if conf[1] == "Pass":
            if old and old.get("source") == "live":
                log["picks"].remove(old); by_id.pop(pid, None)
            continue
        side, line, price = pick_of(g, kind, conf)
        e = {"id": pid, "game": g["id"], "date": board["day"], "matchup": f"{g['away']['abbr']} @ {g['home']['abbr']}",
             "kind": kind, "tier": conf[1].split()[0], "call": conf[1].split(" ", 1)[1], "score": conf[0], "side": side,
             "line": line, "price": price, "source": "live", "result": None}
        if old:
            old.update(e)
        else:
            log["picks"].append(e); by_id[pid] = e

# Grade finished games.
for e in log["picks"]:
    if e.get("result"):
        continue
    st, sc = state(e["game"])
    if st == "post" and None not in sc.values() and len(sc) == 2:
        e["result"] = grade(e, sc)
        e["locked"] = True

log["updated"] = datetime.now(ET).strftime("%b %-d, %-I:%M %p ET")
json.dump(log, open(LOG, "w"), separators=(",", ":"))
board["log"] = log["picks"]
json.dump(board, open(PAGE, "w"), separators=(",", ":"))
print("nhl log", len(log["picks"]), "picks", sum(1 for e in log["picks"] if e.get("result")), "graded", file=sys.stderr)
