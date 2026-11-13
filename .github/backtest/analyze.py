import csv, sys
from collections import defaultdict

rows = [r for r in csv.DictReader(open(sys.argv[1])) if r["total"] not in ("", "None") and r["away_pts"] not in ("", "None")]
f = lambda x: float(x) if x not in ("", "None", None) else None
for r in rows:
    r["T"] = f(r["total"]); r["pts"] = f(r["away_pts"]) + f(r["home_pts"])
    r["over"] = None if r["pts"] == r["T"] else r["pts"] > r["T"]

# Team history in date order: points for/against, 3rd-down and turnover margin per game
hist = defaultdict(list)
for r in sorted(rows, key=lambda r: r["date"]):
    for me, op in (("away", "home"), ("home", "away")):
        tid = (r["lg"], r["season"], r[me])
        r[me + "_prev"] = list(hist[tid])
        c, a, to, oto = f(r[me + "_3c"]), f(r[me + "_3a"]), f(r[me + "_to"]), f(r[op + "_to"])
        hist[tid].append({"pf": f(r[me + "_pts"]), "pa": f(r[op + "_pts"]), "3c": c, "3a": a,
                          "tom": (oto - to) if None not in (to, oto) else None, "date": r["date"], "week": r["week"]})


def l3(prev):
    p = prev[-3:]
    if len(p) < 3:
        return None
    c = sum(x["3c"] or 0 for x in p); a = sum(x["3a"] or 0 for x in p)
    tm = [x["tom"] for x in p if x["tom"] is not None]
    return {"3pct": c / a if a else None, "tom": sum(tm) / len(tm) if tm else None,
            "pf": sum(x["pf"] for x in prev) / len(prev), "pa": sum(x["pa"] for x in prev) / len(prev)}


# Per league-season-week cutoffs: bottom quarter of all teams' L3 3rd-down % and L3 turnover margin
byweek = defaultdict(list)
for r in rows:
    for s in ("away", "home"):
        v = l3(r[s + "_prev"])
        r[s + "_l3"] = v
        if v and v["3pct"] is not None and v["tom"] is not None:
            byweek[(r["lg"], r["season"], r["week"])].append(v)
cut = {}
for k, vs in byweek.items():
    p3 = sorted(v["3pct"] for v in vs); tm = sorted(v["tom"] for v in vs)
    cut[k] = (p3[len(p3) // 4], tm[len(tm) // 4])


def stalled(r, s):
    v = r[s + "_l3"]; c = cut.get((r["lg"], r["season"], r["week"]))
    return bool(v and c and v["3pct"] is not None and v["tom"] is not None and v["3pct"] <= c[0] and v["tom"] <= c[1])


def proj(r):
    a, h = r["away_l3"], r["home_l3"]
    if not a or not h:
        return None
    return (a["pf"] + h["pa"]) / 2 + (h["pf"] + a["pa"]) / 2


def rate(sub, label):
    s = [r for r in sub if r["over"] is not None]
    if not s:
        print(f"  {label:58s} n=0"); return
    w = sum(r["over"] for r in s)
    print(f"  {label:58s} n={len(s):4d}  overs {w}-{len(s) - w}  ({100 * w / len(s):.0f}%)")


for lg in ("cfb", "nfl"):
    R = [r for r in rows if r["lg"] == lg]
    out = [r for r in R if r["indoor"] != "True" and r["gust"] not in ("", "None")]
    print(f"\n=== {lg.upper()} ({len(R)} games with a total; {len(out)} outdoor with weather) ===")
    print("WIND (kickoff + 2 hours, max)")
    rate(out, "all outdoor games")
    for lo, hi in ((0, 15), (15, 20), (20, 25), (25, 30), (30, 99)):
        rate([r for r in out if lo <= f(r["gust"]) < hi], f"gusts {lo}-{hi} mph")
    for lo in (10, 15):
        rate([r for r in out if f(r["wind"]) >= lo], f"sustained wind {lo}+ mph")
    lean = [r for r in out if proj(r) is not None and proj(r) - r["T"] >= 3]
    print("  -- games where scoring averages project 3+ over the total (our over profile):")
    rate(lean, "all")
    rate([r for r in lean if f(r["gust"]) < 20], "gusts under 20")
    rate([r for r in lean if f(r["gust"]) >= 20], "gusts 20+")
    rate([r for r in lean if f(r["gust"]) >= 25], "gusts 25+")
    print("STALLED DRIVES (bottom quarter in BOTH last-3 3rd-down % and turnover margin)")
    rate([r for r in R if r["away_l3"] and r["home_l3"]], "all games with 3+ prior games")
    rate([r for r in R if stalled(r, "away") or stalled(r, "home")], "either offense stalled")
    rate([r for r in R if stalled(r, "away") and stalled(r, "home")], "both stalled")
    lean = [r for r in R if proj(r) is not None and proj(r) - r["T"] >= 3]
    print("  -- our over profile:")
    rate(lean, "all")
    rate([r for r in lean if not (stalled(r, "away") or stalled(r, "home"))], "no stalled offense")
    rate([r for r in lean if stalled(r, "away") or stalled(r, "home")], "a stalled offense")
    print("  -- by season (over profile, stalled vs not):")
    for yr in ("2024", "2025", "2026"):
        L = [r for r in lean if r["season"] == yr]
        rate([r for r in L if stalled(r, "away") or stalled(r, "home")], f"{yr} stalled")
        rate([r for r in L if not (stalled(r, "away") or stalled(r, "home"))], f"{yr} not stalled")
