#!/usr/bin/env python3
"""GarDone — standalone preview. Does not touch app.py."""

import json, pathlib, webbrowser, tempfile, datetime, sys, math

# ── Data loading ─────────────────────────────────────────────────────────────

def load_from_sheets():
    try:
        from config import SHEET_ID, CREDS_FILE
        from google.oauth2.service_account import Credentials
        import gspread
    except ImportError:
        return None
    try:
        creds = Credentials.from_service_account_file(
            CREDS_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        data = {}
        META = {"_notes", "_collabs"}
        for ws in sh.worksheets():
            if ws.title not in META:
                data[ws.title] = ws.get_all_values()
        return data
    except Exception as e:
        print(f"Sheets fetch failed: {e}", file=sys.stderr)
        return None


def load_from_local():
    p = pathlib.Path.home() / ".research-tasks" / "local_data.json"
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f).get("tasks", {})


def collect_done():
    raw = load_from_sheets() or load_from_local()
    done = {}
    for project, rows in raw.items():
        if not rows:
            continue
        tasks, total = [], 0
        for row in rows[1:]:
            t = row[1].strip() if len(row) > 1 else ""
            if not t:
                continue
            total += 1
            status = row[3].strip() if len(row) > 3 else "Not Started"
            if status == "Completed":
                tasks.append({
                    "task":           t,
                    "hours":          row[2].strip() if len(row) > 2 else "",
                    "completed_date": row[4].strip() if len(row) > 4 else "",
                    "assignee":       row[5].strip() if len(row) > 5 else "",
                    "total_tasks":    0,  # filled below
                })
        if tasks:
            for t in tasks:
                t["total_tasks"] = total
            done[project] = {"tasks": tasks, "total": total}
    return done


# ── Flower SVG engine (ported from app.py) ───────────────────────────────────

FLOWER_DEFS = [
    {"center": "#D97706", "fill": "#FBBF24", "stroke": "#F59E0B", "shape": "ellipse"},
    {"center": "#BE185D", "fill": "#FBCFE8", "stroke": "#EC4899", "shape": "round"},
    {"center": "#5B21B6", "fill": "#C4B5FD", "stroke": "#7C3AED", "shape": "teardrop"},
    {"center": "#065F46", "fill": "#6EE7B7", "stroke": "#10B981", "shape": "thin"},
    {"center": "#9B1C1C", "fill": "#FCA5A5", "stroke": "#EF4444", "shape": "wide"},
    {"center": "#1E40AF", "fill": "#93C5FD", "stroke": "#3B82F6", "shape": "diamond"},
]


def _hash(s):
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return h


def _f(v):
    return f"{v:.1f}"


def _petal(shape, length, w, sd, fill, stroke):
    e = sd + length
    mid = sd + length * 0.5
    sw = "0.7"
    if shape == "ellipse":
        return f'<ellipse cx="0" cy="{_f(-mid)}" rx="{_f(w)}" ry="{_f(length*0.5)}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
    if shape == "round":
        return f'<circle cx="0" cy="{_f(-mid)}" r="{_f(length*0.52)}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
    if shape == "teardrop":
        return (f'<path d="M 0 {_f(-sd)} C {_f(w)} {_f(-(sd+length*0.28))} {_f(w*.65)} {_f(-(sd+length*0.78))} '
                f'0 {_f(-e)} C {_f(-w*.65)} {_f(-(sd+length*0.78))} {_f(-w)} {_f(-(sd+length*0.28))} 0 {_f(-sd)} Z" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    if shape == "thin":
        return (f'<path d="M 0 {_f(-sd)} C {_f(w)} {_f(-(sd+length*0.38))} {_f(w*.6)} {_f(-(sd+length*0.75))} '
                f'0 {_f(-e)} C {_f(-w*.6)} {_f(-(sd+length*0.75))} {_f(-w)} {_f(-(sd+length*0.38))} 0 {_f(-sd)} Z" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    if shape == "wide":
        return (f'<path d="M 0 {_f(-sd)} C {_f(w*1.3)} {_f(-(sd+length*0.18))} {_f(w*1.05)} {_f(-(sd+length*0.72))} '
                f'0 {_f(-e)} C {_f(-w*1.05)} {_f(-(sd+length*0.72))} {_f(-w*1.3)} {_f(-(sd+length*0.18))} 0 {_f(-sd)} Z" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    if shape == "diamond":
        return (f'<path d="M 0 {_f(-sd)} L {_f(w)} {_f(-mid)} L 0 {_f(-e)} L {_f(-w)} {_f(-mid)} Z" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    return ""


def flower_svg(project_name, n_tasks, size=140):
    fd = FLOWER_DEFS[_hash(project_name) % len(FLOWER_DEFS)]
    N  = n_tasks
    cx = cy = size / 2
    R  = size * 0.44
    cR = size * 0.10
    SD = cR + size * 0.03
    length = R - SD
    arc_w = (math.pi * (SD + length * 0.5) / N * 0.78) if N > 0 else length * 0.32
    w = max(size * 0.027, min(length * 0.46, arc_w))

    petals = ""
    for i in range(N):
        angle = (360 / N) * i - 90
        petals += (f'<g transform="translate({_f(cx)},{_f(cy)}) rotate({angle:.1f})">'
                   + _petal(fd["shape"], length, w, SD, fd["fill"], fd["stroke"])
                   + "</g>")

    center_fill = fd["fill"]  # all done → lighter center
    check_size = int(cR * 1.4)
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg">'
            f'{petals}'
            f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(cR)}" fill="{fd["center"]}" stroke="rgba(255,255,255,0.5)" stroke-width="1"/>'
            f'<text x="{_f(cx)}" y="{_f(cy + cR*0.42)}" text-anchor="middle" font-size="{check_size}" fill="white">✓</text>'
            f'</svg>')


# ── HTML generation ──────────────────────────────────────────────────────────

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


def specimen_card(project, info):
    tasks     = info["tasks"]
    total     = info["total"]
    done_count = len(tasks)
    hours     = sum(float(t["hours"]) for t in tasks if t["hours"])
    flower    = flower_svg(project, done_count)
    fd        = FLOWER_DEFS[_hash(project) % len(FLOWER_DEFS)]
    accent    = fd["stroke"]
    fill      = fd["fill"]

    task_rows = ""
    for t in tasks:
        date_str = f'<span class="task-date">{_esc(t["completed_date"])}</span>' if t["completed_date"] else ""
        task_rows += f"""
        <li class="task-entry">
          <span class="tick" style="color:{accent}">✓</span>
          <span class="task-name">{_esc(t["task"])}</span>
          {date_str}
        </li>"""

    hours_str = f"{hours:.0f}h logged" if hours else ""
    stats_parts = [f"{done_count} of {total} tasks done"]
    if hours_str:
        stats_parts.append(hours_str)
    stats_str = "  ·  ".join(stats_parts)

    return f"""
    <div class="specimen">
      <div class="specimen-inner">
        <div class="flower-col">
          <div class="flower-wrap" style="border-color:{fill}">
            {flower}
          </div>
          <div class="flower-type-label" style="color:{accent}">{project[0].upper()}</div>
        </div>
        <div class="notes-col">
          <div class="specimen-label" style="border-bottom-color:{accent}">
            <span class="specimen-name">{_esc(project)}</span>
            <span class="specimen-stats">{_esc(stats_str)}</span>
          </div>
          <ul class="task-list">{task_rows}
          </ul>
        </div>
      </div>
      <div class="corner-stamp" style="color:{accent};border-color:{accent}">DONE</div>
    </div>"""


def make_html(done):
    total_done  = sum(len(v["tasks"]) for v in done.values())
    total_hours = sum(
        sum(float(t["hours"]) for t in v["tasks"] if t["hours"])
        for v in done.values()
    )
    today = datetime.date.today().strftime("%d %B %Y")

    cards = "\n".join(specimen_card(p, info) for p, info in sorted(done.items()))

    hours_line = f"<span class='stat-sep'>·</span><span>{total_hours:.0f} hours</span>" if total_hours else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>GarDone</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,600;1,400&family=Crimson+Text:ital@0;1&display=swap');

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: #EDE8DF;
    font-family: 'EB Garamond', 'Palatino Linotype', Georgia, serif;
    color: #2C1F14;
    min-height: 100vh;
    padding: 0 0 80px;
  }}

  /* ── paper texture overlay ── */
  body::before {{
    content: '';
    position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image:
      repeating-linear-gradient(0deg, transparent, transparent 27px, rgba(180,160,120,0.08) 28px);
  }}

  /* ── page header ── */
  .page-header {{
    position: relative; z-index: 1;
    text-align: center;
    padding: 52px 24px 36px;
    border-bottom: 1px solid #C5B99A;
    margin-bottom: 48px;
    background: linear-gradient(to bottom, #F5EFE3, #EDE8DF);
  }}
  .header-top {{
    display: flex; align-items: center; justify-content: center; gap: 16px;
    margin-bottom: 10px;
  }}
  .page-title {{
    font-size: 52px;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: #2C1F14;
    line-height: 1;
  }}
  .page-subtitle {{
    font-size: 16px;
    font-style: italic;
    color: #7A6652;
    margin-bottom: 18px;
  }}
  .page-stats {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-size: 15px;
    color: #5C4A35;
    background: rgba(255,255,255,0.5);
    border: 1px solid #C5B99A;
    border-radius: 40px;
    padding: 6px 20px;
  }}
  .stat-sep {{ color: #B8A990; }}
  .page-date {{
    font-size: 13px;
    color: #A08060;
    margin-top: 14px;
    font-style: italic;
    letter-spacing: 0.04em;
  }}

  /* ── grid ── */
  .garden {{
    position: relative; z-index: 1;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(520px, 1fr));
    gap: 28px;
    max-width: 1200px;
    margin: 0 auto;
    padding: 0 32px;
  }}

  /* ── specimen card ── */
  .specimen {{
    position: relative;
    background: #FAF6EE;
    border: 1px solid #D4C5A9;
    border-radius: 4px;
    padding: 28px 28px 24px;
    box-shadow: 2px 3px 12px rgba(100,80,50,0.10), 0 1px 2px rgba(0,0,0,0.04);
    transition: box-shadow 0.2s ease, transform 0.2s ease;
  }}
  .specimen:hover {{
    box-shadow: 4px 6px 24px rgba(100,80,50,0.18);
    transform: translateY(-2px);
  }}
  /* faint pin hole at top-center */
  .specimen::before {{
    content: '';
    position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #C5B99A;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.2);
  }}

  .specimen-inner {{
    display: flex;
    gap: 24px;
    align-items: flex-start;
  }}

  /* ── flower column ── */
  .flower-col {{
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }}
  .flower-wrap {{
    padding: 10px;
    border: 1px solid;
    border-radius: 50%;
    background: rgba(255,255,255,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .flower-type-label {{
    font-size: 11px;
    font-style: italic;
    opacity: 0.5;
    letter-spacing: 0.05em;
  }}

  /* ── notes column ── */
  .notes-col {{
    flex: 1;
    min-width: 0;
  }}
  .specimen-label {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 9px;
    border-bottom: 1px solid;
    margin-bottom: 14px;
  }}
  .specimen-name {{
    font-size: 20px;
    font-weight: 600;
    color: #2C1F14;
    line-height: 1.2;
    flex: 1;
  }}
  .specimen-stats {{
    font-size: 11px;
    font-style: italic;
    color: #8C7A65;
    white-space: nowrap;
    flex-shrink: 0;
  }}

  /* ── task list ── */
  .task-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  .task-entry {{
    display: flex;
    align-items: baseline;
    gap: 7px;
    font-size: 14px;
    line-height: 1.45;
    color: #3D2B1F;
    padding-bottom: 6px;
    border-bottom: 1px solid rgba(180,160,120,0.2);
  }}
  .task-entry:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .tick {{
    font-size: 11px;
    flex-shrink: 0;
    margin-top: 2px;
  }}
  .task-name {{
    flex: 1;
  }}
  .task-date {{
    font-size: 11px;
    font-style: italic;
    color: #A08060;
    flex-shrink: 0;
    white-space: nowrap;
  }}

  /* ── corner stamp ── */
  .corner-stamp {{
    position: absolute;
    bottom: 14px; right: 16px;
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.2em;
    border: 1px solid;
    border-radius: 2px;
    padding: 2px 5px;
    opacity: 0.25;
    transform: rotate(-2deg);
  }}

  /* ── empty state ── */
  .empty {{
    grid-column: 1 / -1;
    text-align: center;
    color: #A08060;
    font-style: italic;
    font-size: 18px;
    padding: 60px 0;
  }}
</style>
</head>
<body>

<div class="page-header">
  <div class="header-top">
    <div class="page-title">GarDone</div>
  </div>
  <div class="page-subtitle">A record of things completed</div>
  <div class="page-stats">
    <span>{total_done} tasks</span>
    {hours_line}
    <span class="stat-sep">·</span>
    <span>{len(done)} projects</span>
  </div>
  <div class="page-date">{today}</div>
</div>

<div class="garden">
  {''.join(specimen_card(p, info) for p, info in sorted(done.items()))
    if done else '<div class="empty">No completed tasks yet.</div>'}
</div>

</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching data…")
    done = collect_done()
    total = sum(len(v["tasks"]) for v in done.values())

    if not total:
        print("No completed tasks found.")
        sys.exit(0)

    print(f"{total} completed tasks across {len(done)} project(s).")
    html = make_html(done)

    out = pathlib.Path(tempfile.mktemp(suffix=".html", prefix="gardone_"))
    out.write_text(html, encoding="utf-8")
    print(f"Opening {out}")
    webbrowser.open(f"file://{out}")
