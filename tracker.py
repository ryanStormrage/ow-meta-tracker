"""
Overwatch Meta Tracker
----------------------
Pulls hero pick / win / ban rates from the open-source OverFast API
(https://overfast-api.tekrop.fr), stores a daily snapshot in SQLite, and
renders a static dashboard to docs/index.html (ready for GitHub Pages).

Standard library only: no pip install needed.

Usage:
    python tracker.py                         # competitive, PC, Americas
    python tracker.py --region europe
    python tracker.py --division grandmaster
    python tracker.py --no-fetch              # just rebuild the dashboard
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sqlite3
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://overfast-api.tekrop.fr"
USER_AGENT = "ow-meta-tracker/1.0 (+https://github.com/ryanstormrage/ow-meta-tracker)"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "meta.db"
OUT_PATH = ROOT / "docs" / "index.html"

ROLES = ("tank", "damage", "support")
DIVISIONS = ("bronze", "silver", "gold", "platinum", "emerald", "diamond", "master", "grandmaster")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def get_json(path: str, params: dict | None = None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_heroes() -> dict[str, dict]:
    """key -> {name, role, portrait}"""
    return {h["key"]: {"name": h["name"], "role": h["role"], "portrait": h.get("portrait", "")}
            for h in get_json("/heroes")}


def fetch_stats(platform: str, gamemode: str, region: str, division: str | None) -> list[dict]:
    return get_json("/heroes/stats", {
        "platform": platform, "gamemode": gamemode, "region": region,
        "competitive_division": division, "order_by": "pickrate:desc",
    })


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS heroes (
    key       TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL,
    portrait  TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    taken_on  TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    platform  TEXT NOT NULL,
    gamemode  TEXT NOT NULL,
    region    TEXT NOT NULL,
    division  TEXT NOT NULL,          -- 'all' when no division filter
    hero      TEXT NOT NULL REFERENCES heroes(key),
    pickrate  REAL,
    winrate   REAL,
    banrate   REAL,
    PRIMARY KEY (taken_on, platform, gamemode, region, division, hero)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save_snapshot(con, heroes, stats, scope: dict, taken_on: str) -> int:
    con.executemany(
        "INSERT INTO heroes(key,name,role,portrait) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET name=excluded.name, role=excluded.role, portrait=excluded.portrait",
        [(k, v["name"], v["role"], v["portrait"]) for k, v in heroes.items()],
    )
    rows = [
        (taken_on, scope["platform"], scope["gamemode"], scope["region"], scope["division"],
         s["hero"], s.get("pickrate"), s.get("winrate"), s.get("banrate"))
        for s in stats if s.get("hero") in heroes
    ]
    con.executemany(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def meta_score(winrate: float, pickrate: float, banrate: float, avg_pick: float) -> float:
    """
    One number for "how strong is this hero right now".
    Win rate relative to 50% is the core signal; popularity (pick + ban)
    relative to the role average adds confidence that the hero is actually
    being played and feared, not just winning in a tiny sample.
    """
    popularity = (pickrate + 0.5 * (banrate or 0)) / avg_pick if avg_pick else 1
    return round((winrate - 50) * 2 + 10 * (popularity ** 0.5), 2)


def assign_tiers(rows: list[dict]) -> None:
    """S/A/B/C/D by score percentile within each role."""
    for role in ROLES:
        group = sorted([r for r in rows if r["role"] == role], key=lambda r: r["score"], reverse=True)
        n = len(group)
        for i, r in enumerate(group):
            pct = i / max(n - 1, 1)
            r["tier"] = "S" if pct < .12 else "A" if pct < .35 else "B" if pct < .65 else "C" if pct < .88 else "D"


def build_view(con, scope: dict) -> dict:
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT taken_on FROM snapshots WHERE platform=? AND gamemode=? AND region=? AND division=? "
        "ORDER BY taken_on DESC", (scope["platform"], scope["gamemode"], scope["region"], scope["division"]))]
    if not dates:
        return {"dates": [], "rows": []}
    latest = dates[0]
    # compare against the oldest snapshot within the last ~7 days, else the previous one
    target = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
    older = [d for d in dates[1:] if d >= target]
    baseline = older[-1] if older else (dates[1] if len(dates) > 1 else None)

    def load(day):
        return {r["hero"]: dict(r) for r in con.execute(
            "SELECT s.*, h.name, h.role, h.portrait FROM snapshots s JOIN heroes h ON h.key = s.hero "
            "WHERE taken_on=? AND platform=? AND gamemode=? AND region=? AND division=?",
            (day, scope["platform"], scope["gamemode"], scope["region"], scope["division"]))}

    now = load(latest)
    before = load(baseline) if baseline else {}

    rows = list(now.values())
    for role in ROLES:
        picks = [r["pickrate"] or 0 for r in rows if r["role"] == role]
        avg = statistics.mean(picks) if picks else 0
        for r in rows:
            if r["role"] == role:
                r["score"] = meta_score(r["winrate"] or 50, r["pickrate"] or 0, r["banrate"] or 0, avg)
    for r in rows:
        prev = before.get(r["hero"])
        r["d_win"] = round(r["winrate"] - prev["winrate"], 1) if prev and prev["winrate"] is not None else None
        r["d_pick"] = round(r["pickrate"] - prev["pickrate"], 1) if prev and prev["pickrate"] is not None else None
    assign_tiers(rows)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"latest": latest, "baseline": baseline, "dates": dates, "rows": rows}


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def fmt_delta(v):
    if v is None:
        return '<span class="flat">–</span>'
    cls = "up" if v > 0 else "down" if v < 0 else "flat"
    sign = "+" if v > 0 else ""
    return f'<span class="{cls}">{sign}{v:.1f}</span>'


def render(view: dict, scope: dict) -> str:
    e = html.escape
    rows = view["rows"]
    scope_label = f'{scope["gamemode"].title()} · {scope["platform"].upper()} · {scope["region"].title()}' + \
                  (f' · {scope["division"].title()}' if scope["division"] != "all" else " · All ranks")

    movers = sorted([r for r in rows if r["d_win"] is not None], key=lambda r: r["d_win"])
    risers, fallers = movers[::-1][:3], movers[:3]

    def card(r):
        return (f'<div class="hero-card tier-{r["tier"]}"><img src="{e(r["portrait"])}" alt="" loading="lazy">'
                f'<div><b>{e(r["name"])}</b><small>{r["winrate"]:.1f}% WR · {r["pickrate"]:.1f}% PR</small></div>'
                f'<span class="tier">{r["tier"]}</span></div>')

    top_by_role = "".join(
        f'<section class="role-col"><h3>{role.title()}</h3>' +
        "".join(card(r) for r in [x for x in rows if x["role"] == role][:4]) + "</section>"
        for role in ROLES)

    def mover_list(items):
        if not items:
            return '<p class="muted">Needs at least two snapshots. Run the tracker again tomorrow.</p>'
        return "<ul>" + "".join(f'<li><span>{e(r["name"])}</span>{fmt_delta(r["d_win"])}</li>' for r in items) + "</ul>"

    table_rows = "".join(
        f'<tr data-role="{r["role"]}"><td><span class="pill tier-{r["tier"]}">{r["tier"]}</span></td>'
        f'<td class="hero"><img src="{e(r["portrait"])}" alt="" loading="lazy">{e(r["name"])}</td>'
        f'<td>{e(r["role"]).title()}</td>'
        f'<td data-v="{r["winrate"]}">{r["winrate"]:.1f}%</td>'
        f'<td data-v="{r["pickrate"]}">{r["pickrate"]:.1f}%</td>'
        f'<td data-v="{r["banrate"] or 0}">{(r["banrate"] or 0):.1f}%</td>'
        f'<td data-v="{r["score"]}">{r["score"]:.1f}</td>'
        f'<td data-v="{r["d_win"] if r["d_win"] is not None else 0}">{fmt_delta(r["d_win"])}</td></tr>'
        for r in rows)

    since = f'vs {view["baseline"]}' if view.get("baseline") else "first snapshot"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Overwatch Meta Tracker</title>
<style>
:root{{--bg:#0e1116;--panel:#171b22;--line:#29303b;--text:#eef1f5;--muted:#8d96a5;--orange:#f99e1a;--up:#34d399;--down:#f87171;
--S:#f99e1a;--A:#facc15;--B:#4fd1c5;--C:#60a5fa;--D:#6b7280}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 16px 60px}}
h1{{margin:0;font-size:2rem;letter-spacing:-.02em}}h1 span{{color:var(--orange)}}
.sub{{color:var(--muted);margin:4px 0 28px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
.role-col,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}}
.role-col h3,.panel h3{{margin:0 0 10px;font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
.hero-card{{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}}
.hero-card:first-of-type{{border-top:0}}.hero-card img{{width:40px;height:40px;border-radius:8px;background:#222}}
.hero-card div{{flex:1;display:flex;flex-direction:column}}.hero-card small{{color:var(--muted)}}
.tier,.pill{{font-weight:700;width:26px;height:26px;display:inline-grid;place-items:center;border-radius:6px;color:#111}}
.tier-S .tier,.pill.tier-S{{background:var(--S)}}.tier-A .tier,.pill.tier-A{{background:var(--A)}}
.tier-B .tier,.pill.tier-B{{background:var(--B)}}.tier-C .tier,.pill.tier-C{{background:var(--C)}}.tier-D .tier,.pill.tier-D{{background:var(--D)}}
.movers{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0 28px}}
.panel ul{{list-style:none;margin:0;padding:0}}.panel li{{display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid var(--line)}}
.panel li:first-child{{border-top:0}}
.up{{color:var(--up)}}.down{{color:var(--down)}}.flat,.muted{{color:var(--muted)}}
.tabs{{display:flex;gap:8px;margin:0 0 12px;flex-wrap:wrap}}.tabs button{{font:inherit;background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:5px 14px;cursor:pointer}}
.tabs button.on{{background:var(--text);color:var(--bg);border-color:var(--text)}}
.table-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:12px}}
table{{width:100%;border-collapse:collapse;min-width:640px}}th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}}
th{{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer;user-select:none;background:var(--panel)}}
td.hero{{display:flex;align-items:center;gap:10px}}td.hero img{{width:28px;height:28px;border-radius:6px}}
tr:last-child td{{border-bottom:0}}
footer{{margin-top:28px;color:var(--muted);font-size:.85rem}}a{{color:var(--orange)}}
@media(max-width:760px){{.grid,.movers{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<h1>Overwatch <span>Meta</span> Tracker</h1>
<p class="sub">{e(scope_label)} · snapshot {e(view.get("latest","–"))} · movement {e(since)}</p>

<div class="grid">{top_by_role}</div>

<div class="movers">
  <div class="panel"><h3>Biggest win-rate risers</h3>{mover_list(risers)}</div>
  <div class="panel"><h3>Biggest win-rate fallers</h3>{mover_list(fallers)}</div>
</div>

<div class="tabs" id="tabs"><button class="on" data-role="all">All</button><button data-role="tank">Tank</button><button data-role="damage">Damage</button><button data-role="support">Support</button></div>
<div class="table-wrap"><table id="t"><thead><tr>
<th>Tier</th><th>Hero</th><th>Role</th><th data-sort>Win rate</th><th data-sort>Pick rate</th><th data-sort>Ban rate</th><th data-sort>Meta score</th><th data-sort>Δ WR</th>
</tr></thead><tbody>{table_rows}</tbody></table></div>

<footer>Data: <a href="https://overfast-api.tekrop.fr">OverFast API</a> (unofficial, sourced from Blizzard's public hero statistics).
Meta score = win rate above 50% plus a popularity bonus relative to the role average. Tiers are percentiles within each role.
Built by <a href="https://github.com/ryanstormrage">ryanstormrage</a>.</footer>
</div>
<script>
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{{
  document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('#t tbody tr').forEach(tr=>tr.style.display=(b.dataset.role==='all'||tr.dataset.role===b.dataset.role)?'':'none');
}});
document.querySelectorAll('#t th[data-sort]').forEach(th=>{{let desc=true;th.onclick=()=>{{
  const i=[...th.parentNode.children].indexOf(th),tb=document.querySelector('#t tbody');
  [...tb.rows].sort((a,b)=>(+b.cells[i].dataset.v - +a.cells[i].dataset.v)*(desc?1:-1)).forEach(r=>tb.appendChild(r));desc=!desc;}};}});
</script></body></html>"""


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Snapshot Overwatch hero stats and build a meta dashboard.")
    ap.add_argument("--platform", default="pc", choices=["pc", "console"])
    ap.add_argument("--gamemode", default="competitive", choices=["competitive", "quickplay"])
    ap.add_argument("--region", default="americas", choices=["americas", "europe", "asia"])
    ap.add_argument("--division", choices=DIVISIONS, help="competitive rank filter (default: all ranks)")
    ap.add_argument("--no-fetch", action="store_true", help="skip the API and rebuild the dashboard from the database")
    args = ap.parse_args()

    scope = {"platform": args.platform, "gamemode": args.gamemode, "region": args.region,
             "division": args.division or "all"}
    con = connect()

    if not args.no_fetch:
        print("Fetching heroes and stats from OverFast API…")
        try:
            heroes = fetch_heroes()
            stats = fetch_stats(args.platform, args.gamemode, args.region, args.division)
        except OSError as err:
            raise SystemExit(f"Couldn't reach the OverFast API ({err}). Check your connection and try again.")
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        n = save_snapshot(con, heroes, stats, scope, today)
        print(f"Saved {n} hero rows for {today}.")

    view = build_view(con, scope)
    if not view["rows"]:
        print("No data yet: run without --no-fetch first.")
        return
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(view, scope), encoding="utf-8")
    print(f"Dashboard written to {OUT_PATH.relative_to(ROOT)}")
    print("\nTop 5 right now:")
    for r in view["rows"][:5]:
        print(f'  [{r["tier"]}] {r["name"]:<14} {r["role"]:<8} WR {r["winrate"]:.1f}%  PR {r["pickrate"]:.1f}%')


if __name__ == "__main__":
    main()
