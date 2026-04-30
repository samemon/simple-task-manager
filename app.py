#!/usr/bin/env python3
"""Browser-based task manager for research Google Sheet."""

import time
import datetime
import threading
import webbrowser
import json
import os
import re
import pathlib
import shutil
from flask import Flask, jsonify, request, render_template_string

try:
    from google.oauth2.service_account import Credentials
    import gspread
    _GSPREAD_AVAILABLE = True
except ImportError:
    _GSPREAD_AVAILABLE = False

try:
    from config import SHEET_ID, CREDS_FILE
except (ImportError, AttributeError):
    import os
    SHEET_ID   = os.environ.get("SHEET_ID", "")
    CREDS_FILE = os.environ.get("CREDS_FILE", "service_account.json")
TASK_HEADERS   = ["Deadline", "Task", "Hours", "Status", "Completed Date", "Assignee"]
_DATA_DIR = pathlib.Path.home() / ".research-tasks"
_DATA_DIR.mkdir(exist_ok=True)
LOCAL_DATA_FILE = str(_DATA_DIR / "local_data.json")
# One-time migration: move old in-folder local_data.json to the user data dir
_OLD_LOCAL = pathlib.Path("local_data.json")
if _OLD_LOCAL.exists() and not pathlib.Path(LOCAL_DATA_FILE).exists():
    shutil.move(str(_OLD_LOCAL), LOCAL_DATA_FILE)

LOCAL_MODE      = not bool(SHEET_ID)   # True when no Sheet ID configured

SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
STATUSES = ["Not Started", "In Progress", "Pending", "Completed"]
ACTIVE_STATUSES = {"Not Started", "In Progress", "Pending"}
DATA_CACHE_TTL = float('inf')  # never auto-expire; use /api/sync to force refresh

NOTES_SHEET    = "_notes"
COLLABS_SHEET  = "_collabs"
META_SHEETS    = {NOTES_SHEET, COLLABS_SHEET}
NOTE_COLORS    = ["#FFF9C4", "#C8E6C9", "#BBDEFB", "#F8BBD0", "#E1BEE7", "#FFE0B2"]
IMPORTANCES    = ["High", "Medium", "Low"]
PURPOSES       = ["Design", "Writing", "Analysis", "Planning", "Other"]
NOTE_HEADERS   = ["Project", "Note", "Importance", "Purpose", "Color", "Created", "Modified"]
COLLAB_HEADERS = ["Project", "Name", "Role"]

app = Flask(__name__)
_sheet_cache  = None
_fetch_lock   = threading.Lock()
_ws_cache     = {}    # {title: worksheet object}
_data_cache   = {}    # {title: [rows]} — project sheets only
_notes_cache  = []    # rows from _notes sheet
_collabs_cache= []    # rows from _collabs sheet
_data_cache_ts= 0.0


# ── Local-mode storage shim ───────────────────────────────────────────────

class _LocalWS:
    """Mimics a gspread Worksheet so mutation routes work unchanged in local mode."""
    def __init__(self, title, is_notes=False, is_collabs=False):
        self.title     = title
        self._notes    = is_notes
        self._collabs  = is_collabs

    def _row_num(self, range_name):
        m = re.search(r'\d+', range_name)
        return int(m.group()) if m else 1

    def update(self, range_name, values):
        row = self._row_num(range_name)
        if self._notes:
            while len(_notes_cache) < row: _notes_cache.append([])
            _notes_cache[row - 1] = list(values[0])
        elif self._collabs:
            while len(_collabs_cache) < row: _collabs_cache.append([])
            _collabs_cache[row - 1] = list(values[0])
        else:
            patch_cache(self.title, row, values[0])
        _local_save()

    def append_row(self, values):
        if self._notes:    _notes_cache.append(list(values))
        elif self._collabs: _collabs_cache.append(list(values))
        else: _data_cache.setdefault(self.title, []).append(list(values))
        _local_save()

    def delete_rows(self, row):
        target = (_notes_cache if self._notes else
                  _collabs_cache if self._collabs else
                  _data_cache.get(self.title, []))
        if 0 < row <= len(target):
            target.pop(row - 1)
        _local_save()


def _local_load():
    global _data_cache, _notes_cache, _collabs_cache, _data_cache_ts, _ws_cache
    if os.path.exists(LOCAL_DATA_FILE):
        with open(LOCAL_DATA_FILE) as f:
            d = json.load(f)
        _data_cache    = d.get("projects", {})
        _notes_cache   = d.get("notes",    [])
        _collabs_cache = d.get("collabs",  [])
    _data_cache_ts = time.time()
    _ws_cache = {k: _LocalWS(k) for k in _data_cache}


def _local_save():
    with open(LOCAL_DATA_FILE, "w") as f:
        json.dump({"projects": _data_cache, "notes": _notes_cache, "collabs": _collabs_cache}, f, indent=2)


# ── Google Sheets helpers ─────────────────────────────────────────────────

def get_sheet():
    global _sheet_cache
    if _sheet_cache is None:
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        gc = gspread.authorize(creds)
        _sheet_cache = gc.open_by_key(SHEET_ID)
    return _sheet_cache


def _fetch_all():
    """Fetch all sheets in one pass; populate project, notes, and collab caches."""
    global _ws_cache, _data_cache, _notes_cache, _collabs_cache, _data_cache_ts
    if LOCAL_MODE:
        _local_load()
        return
    with _fetch_lock:
        if _data_cache_ts > 0 and (time.time() - _data_cache_ts) < DATA_CACHE_TTL:
            return
        sh = get_sheet()
        for attempt in range(5):
            try:
                worksheets = sh.worksheets()
                _ws_cache = {ws.title: ws for ws in worksheets}
                raw = {ws.title: ws.get_all_values() for ws in worksheets}
                _data_cache   = {k: v for k, v in raw.items() if k not in META_SHEETS}
                _notes_cache  = raw.get(NOTES_SHEET,  [])
                _collabs_cache= raw.get(COLLABS_SHEET, [])
                _data_cache_ts = time.time()
                return
            except gspread.exceptions.APIError as e:
                if e.response.status_code == 429 and attempt < 4:
                    time.sleep(2 ** attempt)
                else:
                    raise


def get_all_sheet_data():
    if _data_cache_ts > 0 and (time.time() - _data_cache_ts) < DATA_CACHE_TTL:
        return _data_cache
    _fetch_all()
    return _data_cache


def get_notes_data():
    if _data_cache_ts > 0 and (time.time() - _data_cache_ts) < DATA_CACHE_TTL:
        return _notes_cache
    _fetch_all()
    return _notes_cache


def get_collabs_data():
    if _data_cache_ts > 0 and (time.time() - _data_cache_ts) < DATA_CACHE_TTL:
        return _collabs_cache
    _fetch_all()
    return _collabs_cache


def get_worksheet(title):
    if LOCAL_MODE:
        if title not in _data_cache:
            _fetch_all()
        if title not in _data_cache:
            raise KeyError(title)
        return _LocalWS(title)
    if title not in _ws_cache:
        _fetch_all()
    if title not in _ws_cache:
        raise KeyError(title)
    return _ws_cache[title]


def ensure_meta_ws(title, headers):
    """Get or lazily create a meta worksheet."""
    global _notes_cache, _collabs_cache
    if LOCAL_MODE:
        is_n = (title == NOTES_SHEET)
        is_c = (title == COLLABS_SHEET)
        if is_n and not _notes_cache:
            _notes_cache = [headers]
            _local_save()
        elif is_c and not _collabs_cache:
            _collabs_cache = [headers]
            _local_save()
        return _LocalWS(title, is_notes=is_n, is_collabs=is_c)
    if title not in _ws_cache:
        _fetch_all()
    if title not in _ws_cache:
        sh = get_sheet()
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.update(range_name="A1", values=[headers])
        _ws_cache[title] = ws
        if title == NOTES_SHEET:
            _notes_cache = [headers]
        elif title == COLLABS_SHEET:
            _collabs_cache = [headers]
    return _ws_cache[title]


def invalidate_cache():
    global _data_cache, _notes_cache, _collabs_cache, _data_cache_ts
    _data_cache    = {}
    _notes_cache   = []
    _collabs_cache = []
    _data_cache_ts = 0.0


def patch_cache(sheet_name, row, values):
    """Update a single task row in-memory without an API read."""
    if sheet_name not in _data_cache:
        return
    rows = _data_cache[sheet_name]
    idx = row - 1
    while len(rows) <= idx:
        rows.append([])
    while len(rows[idx]) < len(values):
        rows[idx].append("")
    rows[idx] = list(values)


def _yesterday():
    return (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d %b %Y")

def _today():
    return datetime.date.today().strftime("%d %b %Y")


def parse_tasks(rows):
    tasks = []
    for i, row in enumerate(rows[1:], start=2):
        task = row[1].strip() if len(row) > 1 else ""
        if not task:
            continue
        status = row[3].strip() if len(row) > 3 else "Not Started"
        completed_date = row[4].strip() if len(row) > 4 else ""
        if status == "Completed" and not completed_date:
            completed_date = _yesterday()
        tasks.append({
            "row": i,
            "deadline": row[0].strip() if len(row) > 0 else "",
            "task": task,
            "hours": row[2].strip() if len(row) > 2 else "",
            "status": status,
            "completed_date": completed_date,
            "assignee": row[5].strip() if len(row) > 5 else "",
        })
    return tasks


def parse_notes(rows):
    notes = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 2 or not row[1].strip():
            continue
        notes.append({
            "row": i,
            "project":    row[0].strip() if len(row) > 0 else "",
            "note":       row[1].strip(),
            "importance": row[2].strip() if len(row) > 2 else "Medium",
            "purpose":    row[3].strip() if len(row) > 3 else "Other",
            "color":      row[4].strip() if len(row) > 4 else NOTE_COLORS[0],
            "created":    row[5].strip() if len(row) > 5 else "",
            "modified":   row[6].strip() if len(row) > 6 else "",
        })
    return notes


def parse_collabs(rows):
    collabs = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 2 or not row[1].strip():
            continue
        collabs.append({
            "row":     i,
            "project": row[0].strip() if len(row) > 0 else "",
            "name":    row[1].strip(),
            "role":    row[2].strip() if len(row) > 2 else "",
        })
    return collabs


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/sheets")
def api_sheets():
    data = get_all_sheet_data()
    result = []
    for title, rows in data.items():
        tasks = parse_tasks(rows)
        result.append({
            "name": title,
            "active": sum(1 for t in tasks if t["status"] in ACTIVE_STATUSES),
            "total": len(tasks),
        })
    return jsonify(result)


@app.route("/api/tasks")
def api_tasks():
    sheet_filter = request.args.get("sheet")
    status_filter = request.args.getlist("status") or None
    data = get_all_sheet_data()
    result = {}
    for title, rows in data.items():
        if sheet_filter and title != sheet_filter:
            continue
        tasks = parse_tasks(rows)
        if status_filter:
            tasks = [t for t in tasks if t["status"] in status_filter]
        if tasks:
            result[title] = tasks
    return jsonify(result)


@app.route("/api/tasks", methods=["POST"])
def api_add_task():
    data = request.get_json(silent=True) or {}
    if not data.get("task") or not data.get("sheet"):
        return jsonify({"error": "sheet and task required"}), 400
    try:
        ws = get_worksheet(data["sheet"])
    except KeyError:
        return jsonify({"error": "Sheet not found"}), 404

    cached_rows = get_all_sheet_data().get(data["sheet"], [])
    col_b = [r[1] if len(r) > 1 else "" for r in cached_rows]
    next_row = len(col_b) + 1
    for i, val in enumerate(col_b[1:], start=2):
        if not val.strip():
            next_row = i
            break

    status = data.get("status", "Not Started")
    completed_date = _today() if status == "Completed" else ""
    new_row = [data.get("deadline", ""), data["task"], data.get("hours", ""),
               status, completed_date, data.get("assignee", "")]
    ws.update(range_name=f"A{next_row}:F{next_row}", values=[new_row])
    patch_cache(data["sheet"], next_row, new_row)
    return jsonify({"row": next_row, "task": data["task"], "status": status})


@app.route("/api/tasks/<sheet_name>/<int:row>", methods=["PUT"])
def api_update_task(sheet_name, row):
    data = request.get_json(silent=True) or {}
    try:
        ws = get_worksheet(sheet_name)
    except KeyError:
        return jsonify({"error": "Sheet not found"}), 404

    sheet_rows = get_all_sheet_data().get(sheet_name, [])
    current = list(sheet_rows[row - 1]) if row - 1 < len(sheet_rows) else []
    while len(current) < 6:
        current.append("")

    old_status = current[3]
    new_status  = data.get("status", old_status)
    if new_status == "Completed" and old_status != "Completed":
        completed_date = _today()
    else:
        completed_date = current[4]

    updated = [
        data.get("deadline",  current[0]),
        data.get("task",      current[1]),
        data.get("hours",     current[2]),
        new_status,
        completed_date,
        data.get("assignee",  current[5]),
    ]
    ws.update(range_name=f"A{row}:F{row}", values=[updated])
    patch_cache(sheet_name, row, updated)
    return jsonify({"row": row, "updated": updated})


@app.route("/api/tasks/<sheet_name>/<int:row>", methods=["DELETE"])
def api_delete_task(sheet_name, row):
    try:
        ws = get_worksheet(sheet_name)
    except KeyError:
        return jsonify({"error": "Sheet not found"}), 404
    cleared = ["", "", "", "Not Started", "", ""]
    ws.update(range_name=f"A{row}:F{row}", values=[cleared])
    patch_cache(sheet_name, row, cleared)
    return jsonify({"deleted": row})


# ── Notes API ────────────────────────────────────────────────────────────────

@app.route("/api/notes")
def api_notes():
    project = request.args.get("project")
    notes = parse_notes(get_notes_data())
    if project:
        notes = [n for n in notes if n["project"] == project]
    return jsonify(notes)


@app.route("/api/notes", methods=["POST"])
def api_add_note():
    data = request.get_json(silent=True) or {}
    ws = ensure_meta_ws(NOTES_SHEET, NOTE_HEADERS)
    today = _today()
    ws.append_row([
        data.get("project", ""), data.get("note", ""),
        data.get("importance", "Medium"), data.get("purpose", "Other"),
        data.get("color", NOTE_COLORS[0]), today, today,
    ])
    invalidate_cache()
    return jsonify({"ok": True})


@app.route("/api/notes/<int:row>", methods=["PUT"])
def api_update_note(row):
    data = request.get_json(silent=True) or {}
    ws = ensure_meta_ws(NOTES_SHEET, NOTE_HEADERS)
    rows = get_notes_data()
    current = list(rows[row - 1]) if row - 1 < len(rows) else []
    while len(current) < 7:
        current.append("")
    updated = [
        data.get("project",    current[0]),
        data.get("note",       current[1]),
        data.get("importance", current[2]),
        data.get("purpose",    current[3]),
        data.get("color",      current[4]),
        current[5],   # created unchanged
        _today(),     # modified = today
    ]
    ws.update(range_name=f"A{row}:G{row}", values=[updated])
    invalidate_cache()
    return jsonify({"ok": True})


@app.route("/api/notes/<int:row>", methods=["DELETE"])
def api_delete_note(row):
    ws = ensure_meta_ws(NOTES_SHEET, NOTE_HEADERS)
    ws.delete_rows(row)
    invalidate_cache()
    return jsonify({"ok": True})


# ── Collaborators API ─────────────────────────────────────────────────────────

@app.route("/api/collaborators")
def api_collaborators():
    project = request.args.get("project")
    collabs = parse_collabs(get_collabs_data())
    if project:
        collabs = [c for c in collabs if c["project"] == project]
    return jsonify(collabs)


@app.route("/api/collaborators", methods=["POST"])
def api_add_collaborator():
    data = request.get_json(silent=True) or {}
    ws = ensure_meta_ws(COLLABS_SHEET, COLLAB_HEADERS)
    names = data.get("names") or ([data.get("name")] if data.get("name") else [])
    role    = data.get("role", "")
    project = data.get("project", "")
    existing = {r[1].strip().lower() for r in get_collabs_data()[1:] if len(r) > 1 and r[0].strip() == project}
    for name in names:
        name = name.strip()
        if name and name.lower() not in existing:
            ws.append_row([project, name, role])
            existing.add(name.lower())
    invalidate_cache()
    return jsonify({"ok": True})


@app.route("/api/collaborators/<int:row>", methods=["DELETE"])
def api_delete_collaborator(row):
    ws = ensure_meta_ws(COLLABS_SHEET, COLLAB_HEADERS)
    ws.delete_rows(row)
    invalidate_cache()
    return jsonify({"ok": True})


# ── Sync API ──────────────────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Force re-fetch (from Sheets or local JSON)."""
    invalidate_cache()
    _fetch_all()
    return jsonify({"ok": True, "local_mode": LOCAL_MODE})


@app.route("/api/status")
def api_status():
    return jsonify({"local_mode": LOCAL_MODE})


# ── Projects API ──────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["POST"])
def api_create_project():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    get_all_sheet_data()   # ensure cache warm
    if name in _data_cache or name in _ws_cache:
        return jsonify({"error": "Project already exists"}), 409
    if LOCAL_MODE:
        _data_cache[name] = [TASK_HEADERS]
        _ws_cache[name]   = _LocalWS(name)
        _local_save()
        return jsonify({"ok": True, "name": name})
    sh = get_sheet()
    ws = sh.add_worksheet(title=name, rows=1000, cols=10)
    ws.update(range_name="A1:F1", values=[TASK_HEADERS])
    _ws_cache[name]   = ws
    _data_cache[name] = [TASK_HEADERS]   # ← bug fix: header row prevents row-1 collision
    return jsonify({"ok": True, "name": name})


@app.route("/api/projects/<name>", methods=["DELETE"])
def api_delete_project(name):
    global _data_cache, _ws_cache
    if name not in _data_cache:
        _fetch_all()
    if name not in _data_cache:
        return jsonify({"error": "Project not found"}), 404
    if name in META_SHEETS:
        return jsonify({"error": "Cannot delete meta sheet"}), 400
    if LOCAL_MODE:
        _data_cache.pop(name, None)
        _ws_cache.pop(name, None)
        _local_save()
        return jsonify({"ok": True})
    sh = get_sheet()
    sh.del_worksheet(_ws_cache[name])
    _ws_cache.pop(name, None)
    _data_cache.pop(name, None)
    return jsonify({"ok": True})


# ── UI ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KaamKaaj</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #F4F6F8;
    --surface: #FFFFFF;
    --sidebar-bg: #1A1A2E;
    --sidebar-text: #A0AEC0;
    --sidebar-active: #FFFFFF;
    --sidebar-hover: rgba(255,255,255,0.08);
    --border: #EAEAEA;
    --text: #1A202C;
    --text-muted: #718096;
    --accent: #5A67D8;
    --accent-light: rgba(90,103,216,0.12);

    --pending-bg: #FFF8E1; --pending-text: #B7791F; --pending-dot: #F6C90E;
    --inprogress-bg: #EBF8FF; --inprogress-text: #2B6CB0; --inprogress-dot: #4299E1;
    --notstarted-bg: #F7FAFC; --notstarted-text: #718096; --notstarted-dot: #CBD5E0;
    --completed-bg: #F0FFF4; --completed-text: #276749; --completed-dot: #48BB78;
    --overdue-color: #C53030; --today-color: #C05621; --week-color: #B7791F;
    --month-color: #2B6CB0; --later-color: #718096;
  }

  /* ── Theme dots ── */
  #theme-switcher { display: flex; gap: 7px; padding: 10px 20px 4px; }
  .theme-dot {
    width: 14px; height: 14px; border-radius: 50%; cursor: pointer;
    border: 2px solid transparent; transition: transform 0.15s, border-color 0.15s;
    flex-shrink: 0;
  }
  .theme-dot:hover { transform: scale(1.25); }
  .theme-dot.active { border-color: rgba(255,255,255,0.7); transform: scale(1.15); }

  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }

  /* Sidebar */
  #sidebar {
    width: 220px; min-width: 160px; max-width: 400px; flex-shrink: 0;
    background: var(--sidebar-bg); display: flex; flex-direction: column;
    overflow-y: auto; position: relative;
  }
  #sidebar-header { padding: 18px 20px 12px; }
  #sidebar-logo { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  #sidebar-logo svg { flex-shrink: 0; }
  #sidebar-header h1 { font-size: 16px; font-weight: 800; color: #FFF; letter-spacing: 0.2px; }
  #sidebar-header p { font-size: 11px; color: var(--sidebar-text); margin-top: 1px; }
  .sheet-item {
    padding: 9px 20px; cursor: pointer; border-radius: 6px; margin: 1px 8px;
    display: flex; align-items: center; justify-content: space-between;
    color: var(--sidebar-text); font-size: 13px; transition: background 0.15s;
  }
  .sheet-item:hover { background: var(--sidebar-hover); color: #fff; }
  .sheet-item.active { background: rgba(90,103,216,0.35); color: var(--sidebar-active); font-weight: 600; }
  .sheet-item .badge { font-size: 10px; background: rgba(255,255,255,0.15);
                        color: #fff; padding: 1px 6px; border-radius: 10px; }
  .sidebar-sep { height: 1px; background: rgba(255,255,255,0.06); margin: 8px 16px; }
  #show-all, #upcoming-btn, #notes-btn, #collabs-btn, #stats-btn, #procrastinate-btn {
    padding: 9px 20px; cursor: pointer; font-size: 13px; color: var(--sidebar-text);
    margin: 1px 8px; border-radius: 6px; display: flex; align-items: center; gap: 8px;
  }
  #show-all:hover, #upcoming-btn:hover, #notes-btn:hover, #collabs-btn:hover, #stats-btn:hover, #procrastinate-btn:hover {
    background: var(--sidebar-hover); color: #fff;
  }
  #show-all.active, #upcoming-btn.active, #notes-btn.active, #collabs-btn.active, #stats-btn.active, #procrastinate-btn.active {
    background: rgba(90,103,216,0.35); color: var(--sidebar-active); font-weight: 600;
  }
  .deadline-pill {
    display: inline-block; font-size: 11px; font-weight: 600;
    padding: 2px 8px; border-radius: 10px; white-space: nowrap;
  }
  .deadline-overdue { background: #FFF5F5; color: var(--overdue-color); }
  .deadline-today   { background: #FFFAF0; color: var(--today-color); }
  .deadline-week    { background: #FEFCBF; color: var(--week-color); }
  .deadline-normal  { background: #EBF8FF; color: var(--month-color); }
  .deadline-later   { background: #F7FAFC; color: var(--later-color); }

  /* Main */
  #main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

  #topbar {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 14px 28px; display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  }
  #topbar h2 { font-size: 16px; font-weight: 700; flex: 1; }
  .filter-btn {
    padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;
    cursor: pointer; border: 1.5px solid transparent; transition: all 0.15s; background: none;
  }
  .filter-btn[data-s="all"]         { border-color: #CBD5E0; color: #718096; }
  .filter-btn[data-s="Pending"]     { border-color: var(--pending-dot); color: var(--pending-text); }
  .filter-btn[data-s="In Progress"] { border-color: var(--inprogress-dot); color: var(--inprogress-text); }
  .filter-btn[data-s="Not Started"] { border-color: var(--notstarted-dot); color: var(--notstarted-text); }
  .filter-btn[data-s="Completed"]   { border-color: var(--completed-dot); color: var(--completed-text); }
  .filter-btn.active[data-s="all"]         { background: #CBD5E0; color: #2D3748; }
  .filter-btn.active[data-s="Pending"]     { background: var(--pending-bg); }
  .filter-btn.active[data-s="In Progress"] { background: var(--inprogress-bg); }
  .filter-btn.active[data-s="Not Started"] { background: var(--notstarted-bg); }
  .filter-btn.active[data-s="Completed"]   { background: var(--completed-bg); }

  #content { flex: 1; overflow-y: auto; padding: 24px 28px; }

  .section { margin-bottom: 28px; }
  .section-title {
    font-size: 11px; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 10px;
  }
  .task-table { width: 100%; border-collapse: collapse; background: var(--surface);
                 border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }
  .task-row { border-bottom: 1px solid var(--border); }
  .task-row:last-child { border-bottom: none; }
  .task-row td { padding: 11px 14px; vertical-align: middle; }
  .task-row:hover { background: #FAFBFC; }

  .task-text { font-size: 14px; color: var(--text); }
  .task-meta { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

  .status-badge {
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 600; white-space: nowrap; cursor: pointer;
  }
  .status-Pending     { background: var(--pending-bg); color: var(--pending-text); }
  .status-In\ Progress{ background: var(--inprogress-bg); color: var(--inprogress-text); }
  .status-Not\ Started{ background: var(--notstarted-bg); color: var(--notstarted-text); }
  .status-Completed   { background: var(--completed-bg); color: var(--completed-text); }

  .icon-btn {
    background: none; border: none; cursor: pointer; padding: 4px 6px;
    color: var(--text-muted); border-radius: 4px; font-size: 14px; transition: all 0.15s;
    opacity: 0;
  }
  .task-row:hover .icon-btn { opacity: 1; }
  .icon-btn:hover { background: var(--border); color: var(--text); }

  #add-btn {
    padding: 8px 18px; background: var(--accent); color: #fff; border: none;
    border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;
    transition: opacity 0.15s;
  }
  #add-btn:hover { opacity: 0.88; }

  #refresh-btn {
    padding: 7px 14px; background: none; border: 1.5px solid var(--border);
    border-radius: 8px; font-size: 13px; cursor: pointer; color: var(--text-muted);
  }
  #refresh-btn:hover { background: var(--border); }

  /* Modal */
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4);
    z-index: 100; align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border-radius: 12px; padding: 28px;
    width: 460px; max-width: 95vw; box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  }
  .modal h3 { font-size: 16px; font-weight: 700; margin-bottom: 20px; }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 12px; font-weight: 600;
                  color: var(--text-muted); margin-bottom: 5px; text-transform: uppercase; }
  .field input, .field select, .field textarea {
    width: 100%; padding: 8px 12px; border: 1.5px solid var(--border);
    border-radius: 7px; font-size: 14px; font-family: inherit; outline: none;
    transition: border-color 0.15s;
  }
  .field input:focus, .field select:focus, .field textarea:focus { border-color: var(--accent); }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
  .btn-cancel { padding: 8px 16px; border: 1.5px solid var(--border); background: none;
                 border-radius: 7px; cursor: pointer; font-size: 13px; }
  .btn-save { padding: 8px 20px; background: var(--accent); color: #fff; border: none;
               border-radius: 7px; cursor: pointer; font-size: 13px; font-weight: 600; }

  .spinner { text-align: center; padding: 60px; color: var(--text-muted); }
  .empty { text-align: center; padding: 40px; color: var(--text-muted); font-size: 14px; }

  /* ── Notes ── */
  .notes-toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 20px; flex-wrap: wrap; }
  .notes-toolbar select { padding: 6px 10px; border: 1.5px solid var(--border); border-radius: 7px;
                           font-size: 13px; font-family: inherit; outline: none; background: var(--surface); }
  .notes-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 16px; }
  .note-card {
    background: var(--surface); border-radius: 10px; padding: 16px 16px 12px;
    border-left: 5px solid #ccc; box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    position: relative; transition: box-shadow 0.15s;
  }
  .note-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.11); }
  .note-header { display: flex; align-items: center; gap: 5px; margin-bottom: 8px; flex-wrap: wrap; }
  .note-project { font-size: 10px; font-weight: 700; color: var(--text-muted);
                   text-transform: uppercase; letter-spacing: 0.6px; flex: 1; }
  .note-badge { font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 8px; }
  .imp-High     { background: #FED7D7; color: #C53030; }
  .imp-Medium   { background: #FEFCBF; color: #B7791F; }
  .imp-Low      { background: #C6F6D5; color: #276749; }
  .pur-Design   { background: #E9D8FD; color: #553C9A; }
  .pur-Writing  { background: #BEE3F8; color: #2C5282; }
  .pur-Analysis { background: #FEEBC8; color: #7B341E; }
  .pur-Planning { background: #C6F6D5; color: #22543D; }
  .pur-Other    { background: #EDF2F7; color: #4A5568; }
  .note-text { font-size: 13px; color: var(--text); line-height: 1.55;
                margin-bottom: 10px; white-space: pre-wrap; word-break: break-word; }
  .note-footer { display: flex; align-items: center; justify-content: space-between;
                  gap: 8px; margin-top: 8px; }
  .note-dates { font-size: 11px; color: var(--text-muted); }
  .note-actions-inline { display: flex; gap: 4px; flex-shrink: 0; }
  .note-action-btn {
    background: rgba(0,0,0,0.06); border: none; border-radius: 5px; cursor: pointer;
    padding: 3px 9px; font-size: 11px; font-weight: 600; color: var(--text-muted);
  }
  .note-action-btn:hover { background: var(--border); color: var(--text); }
  .note-delete-btn:hover { background: #FED7D7; color: #C53030; }

  /* ── Inline project extras ── */
  .project-extras { margin-top: 10px; margin-bottom: 6px; display: flex; flex-direction: column; gap: 8px; }
  .project-extras-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  .collab-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .collab-chip-label, .notes-strip-label {
    font-size: 10px; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .collab-chip {
    display: inline-flex; align-items: center; gap: 4px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 20px;
    padding: 3px 10px; font-size: 12px; color: var(--text);
  }
  .chip-role { color: var(--text-muted); font-size: 11px; }
  .notes-count-pill {
    display: inline-flex; align-items: center; gap: 4px;
    background: var(--accent-light); color: var(--accent);
    border-radius: 12px; padding: 3px 10px; font-size: 12px; font-weight: 600;
    cursor: pointer; border: none;
  }
  .notes-count-pill:hover { opacity: 0.8; }

  /* ── Project note cards (inside task view) ── */
  .project-notes-section { display: flex; flex-direction: column; gap: 6px; }
  .project-notes-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px; }
  .project-note-card {
    background: var(--surface); border-radius: 8px; border-left: 4px solid #ccc;
    padding: 10px 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }
  .project-note-meta { display: flex; align-items: center; gap: 4px; margin-bottom: 5px; flex-wrap: wrap; }
  .project-note-date { font-size: 10px; color: var(--text-muted); margin-left: auto; }
  .project-note-text { font-size: 12px; color: var(--text); line-height: 1.5;
                        margin-bottom: 6px; white-space: pre-wrap; word-break: break-word; }
  .project-note-actions { display: flex; gap: 4px; }
  .more-notes-btn {
    font-size: 11px; color: var(--accent); background: none; border: none;
    cursor: pointer; padding: 2px 0; font-weight: 600;
  }
  .more-notes-btn:hover { text-decoration: underline; }

  /* ── Color swatches ── */
  .color-swatches { display: flex; gap: 8px; flex-wrap: wrap; padding: 4px 0; }
  .color-swatch {
    width: 26px; height: 26px; border-radius: 50%; cursor: pointer;
    border: 2px solid transparent; transition: transform 0.12s, border-color 0.12s;
  }
  .color-swatch:hover { transform: scale(1.2); }
  .color-swatch.selected { border-color: var(--text); transform: scale(1.15); }

  /* ── Collaborators ── */
  .collab-section { margin-bottom: 28px; }
  .collab-table { width: 100%; border-collapse: collapse; background: var(--surface);
                   border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }
  .collab-row { border-bottom: 1px solid var(--border); }
  .collab-row:last-child { border-bottom: none; }
  .collab-row td { padding: 10px 14px; vertical-align: middle; font-size: 13px; }
  .collab-row:hover { background: #FAFBFC; }
  .collab-name { font-weight: 600; color: var(--text); }
  .collab-role { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
  .collab-tasks { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
  .collab-add-row td { padding: 6px 14px; }

  /* ── Sidebar footer ── */
  #sidebar-footer { padding: 10px 12px 16px; }
  #new-project-btn {
    width: 100%; padding: 8px; background: rgba(255,255,255,0.06);
    border: 1px dashed rgba(255,255,255,0.2); border-radius: 6px;
    color: var(--sidebar-text); font-size: 12px; cursor: pointer; text-align: center;
    transition: background 0.15s;
  }
  #new-project-btn:hover { background: rgba(255,255,255,0.15); color: #fff; }

  /* Status cycle dropdown */
  .status-select {
    position: absolute; background: var(--surface); border: 1.5px solid var(--border);
    border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.12);
    z-index: 50; min-width: 140px; overflow: hidden; display: none;
  }
  .status-select.open { display: block; }
  .status-option {
    padding: 9px 14px; cursor: pointer; font-size: 13px;
    display: flex; align-items: center; gap: 8px;
  }
  .status-option:hover { background: var(--bg); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }

  /* ── Garden / flower cards ── */
  .garden-grid { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 28px; }
  .garden-card {
    background: var(--surface); border-radius: 12px; padding: 16px 12px 12px;
    display: flex; flex-direction: column; align-items: center; gap: 7px;
    cursor: pointer; width: 118px; box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    transition: box-shadow 0.15s, transform 0.15s; position: relative;
  }
  .garden-card:hover { box-shadow: 0 6px 20px rgba(0,0,0,0.13); transform: translateY(-2px); }
  .garden-name { font-size: 11px; font-weight: 600; color: var(--text); text-align: center; line-height: 1.3; }
  .garden-progress { font-size: 10px; color: var(--text-muted); }
  .garden-delete {
    position: absolute; top: 5px; right: 5px; background: none; border: none;
    color: var(--text-muted); font-size: 14px; font-weight: 700; cursor: pointer;
    padding: 1px 5px; border-radius: 4px; opacity: 0; transition: opacity 0.15s, background 0.15s; line-height: 1;
  }
  .garden-card:hover .garden-delete { opacity: 1; }
  .garden-delete:hover { background: #FED7D7; color: #C53030; }

  /* ── Section header (with flower) ── */
  .section-header { display: flex; align-items: center; gap: 9px; margin-bottom: 10px; }
  .section-header .section-title { margin-bottom: 0; flex: 1; }
  .section-done { font-size: 11px; color: var(--text-muted); font-weight: 400; white-space: nowrap; }
  .section-del-btn {
    background: none; border: none; cursor: pointer; color: var(--text-muted); font-size: 13px;
    padding: 3px 6px; border-radius: 4px; opacity: 0; transition: opacity 0.15s, background 0.15s;
  }
  .section:hover .section-del-btn { opacity: 1; }
  .section-del-btn:hover { background: #FED7D7; color: #C53030; }

  /* ── Search & export ── */
  #search-input {
    padding: 6px 12px; border: 1.5px solid var(--border); border-radius: 20px;
    font-size: 13px; font-family: inherit; outline: none; width: 190px;
    background: var(--bg); color: var(--text); transition: border-color 0.15s, width 0.2s;
  }
  #search-input:focus { border-color: var(--accent); width: 240px; }
  #search-input::placeholder { color: var(--text-muted); }
  #export-btn {
    padding: 7px 12px; background: none; border: 1.5px solid var(--border);
    border-radius: 8px; font-size: 12px; cursor: pointer; color: var(--text-muted); white-space: nowrap;
  }
  #export-btn:hover { background: var(--border); }

  /* ── Bulk bar ── */
  #bulk-bar {
    position: fixed; bottom: 0; left: var(--sidebar-w, 220px); right: 0;
    background: var(--sidebar-bg); color: #fff;
    padding: 0 28px; display: flex; align-items: center; gap: 12px;
    height: 0; overflow: hidden; transition: height 0.25s ease; z-index: 60;
    box-shadow: 0 -4px 20px rgba(0,0,0,0.2);
  }
  #bulk-bar.visible { height: 52px; }
  #bulk-count { font-size: 13px; font-weight: 600; flex: 1; }
  .bulk-btn {
    padding: 5px 12px; border: 1px solid rgba(255,255,255,0.3); border-radius: 7px;
    background: rgba(255,255,255,0.1); color: #fff; font-size: 12px; font-weight: 600;
    cursor: pointer; white-space: nowrap; transition: background 0.15s;
  }
  .bulk-btn:hover { background: rgba(255,255,255,0.22); }
  .bulk-btn.danger:hover { background: #C53030; border-color: #C53030; }
  .bulk-clear { border-color: rgba(255,255,255,0.15); color: rgba(255,255,255,0.55); }
  .task-check { width: 14px; height: 14px; cursor: pointer; accent-color: var(--accent); flex-shrink: 0; }
  .task-row.selected { background: var(--accent-light); }

  /* ── Stats view ── */
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px,1fr)); gap: 14px; margin-bottom: 24px; }
  .stat-card {
    background: var(--surface); border-radius: 12px; padding: 18px 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07); display: flex; flex-direction: column; gap: 5px;
  }
  .stat-label { font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.7px; }
  .stat-value { font-size: 30px; font-weight: 800; color: var(--accent); line-height: 1; }
  .stat-sub   { font-size: 11px; color: var(--text-muted); }
  .progress-bar { height: 5px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 8px; }
  .progress-fill { height: 100%; background: var(--accent); border-radius: 3px; }
  .proj-stat-row {
    display: flex; align-items: center; gap: 10px; padding: 9px 0;
    border-bottom: 1px solid var(--border); cursor: pointer;
  }
  .proj-stat-row:last-child { border-bottom: none; }
  .proj-stat-row:hover { opacity: 0.8; }
  .proj-stat-name { flex: 1; font-size: 13px; font-weight: 600; color: var(--text); }
  .proj-stat-bar  { flex: 2; }
  .proj-stat-pct  { font-size: 12px; color: var(--text-muted); min-width: 40px; text-align: right; }

  /* ── Sidebar resize handle ── */
  #sidebar-resize {
    position: absolute; right: 0; top: 0; width: 5px; height: 100%;
    cursor: col-resize; z-index: 20; transition: background 0.15s;
    border-radius: 0 3px 3px 0;
  }
  #sidebar-resize:hover, #sidebar-resize.dragging { background: var(--accent-light); }

  /* ── Procrastination view ── */
  .procrastinate-layout { display: flex; gap: 28px; flex-wrap: wrap; align-items: flex-start; }
  .snake-section { display: flex; flex-direction: column; align-items: center; gap: 10px; flex-shrink: 0; }
  .snake-header { width: 420px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  .snake-meta { display: flex; align-items: center; gap: 14px; }
  .snake-score-label { font-size: 13px; color: var(--text-muted); }
  .procrastinate-timer {
    font-size: 24px; font-weight: 800; font-variant-numeric: tabular-nums;
    color: var(--accent); letter-spacing: 1px; min-width: 60px; text-align: center;
  }
  .procrastinate-timer.warning { color: #D4A017; }
  .procrastinate-timer.danger  { color: #FC8181; }
  .snake-controls { display: flex; align-items: center; gap: 8px; }
  .snake-controls select {
    padding: 6px 10px; border: 1.5px solid var(--border); border-radius: 7px;
    font-size: 12px; background: var(--surface); outline: none; cursor: pointer;
  }
  #snake-canvas { border-radius: 12px; display: block; box-shadow: 0 6px 28px rgba(0,0,0,0.35); }
  .snake-hint { font-size: 11px; color: var(--text-muted); text-align: center; }
  .quotes-section { flex: 1; min-width: 260px; max-height: 480px; overflow-y: auto; }
  .quotes-list { display: flex; flex-direction: column; gap: 12px; }
  .quote-card {
    background: var(--surface); border-radius: 10px; padding: 16px 18px;
    border-left: 4px solid var(--accent); box-shadow: 0 1px 4px rgba(0,0,0,0.07);
  }
  .quote-text {
    font-size: 14px; line-height: 1.7; color: var(--text); font-style: italic; margin-bottom: 10px;
  }
  .quote-author {
    font-size: 11px; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.6px;
  }

  /* ── Procrastination timeout overlay ── */
  #procrastinate-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.78);
    z-index: 200; align-items: center; justify-content: center; flex-direction: column;
  }
  #procrastinate-overlay.open { display: flex; }
  .po-box {
    background: var(--surface); border-radius: 18px; padding: 44px 52px;
    max-width: 500px; width: 92vw; text-align: center;
    box-shadow: 0 32px 80px rgba(0,0,0,0.45);
    animation: popin 0.35s cubic-bezier(.175,.885,.32,1.275);
  }
  @keyframes popin { from { transform: scale(0.6); opacity: 0; } to { transform: scale(1); opacity: 1; } }
  #po-emoji  { font-size: 56px; margin-bottom: 14px; }
  #po-text   { font-size: 20px; font-weight: 700; color: var(--text); margin-bottom: 8px; line-height: 1.4; }
  #po-sub    { font-size: 14px; color: var(--text-muted); margin-bottom: 28px; line-height: 1.5; }
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">
    <div id="sidebar-logo">
      <svg width="34" height="34" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
        <g transform="translate(60,60)">
          <g transform="rotate(-90)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(-45)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(0)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(45)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(90)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(135)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(180)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
          <g transform="rotate(225)"><ellipse cx="0" cy="-34" rx="10.5" ry="18.5" fill="#FBBF24" stroke="#F59E0B" stroke-width="0.8"/></g>
        </g>
        <circle cx="60" cy="60" r="13" fill="#D97706" stroke="rgba(255,255,255,0.5)" stroke-width="1.5"/>
      </svg>
      <h1>KaamKaaj</h1>
    </div>
    <p id="total-count">Loading…</p>
  </div>
  <div id="local-mode-badge" style="display:none;margin:4px 10px 0;padding:5px 10px;background:rgba(104,211,145,0.15);border-radius:6px;font-size:11px;color:#68D391;font-weight:600;">
    💾 Local Mode — no Sheets
  </div>
  <div id="theme-switcher">
    <span class="theme-dot active" data-theme="classic"  style="background:#5A67D8" onclick="applyTheme('classic')"  title="Classic"></span>
    <span class="theme-dot"        data-theme="ocean"    style="background:#0072CE" onclick="applyTheme('ocean')"    title="Ocean"></span>
    <span class="theme-dot"        data-theme="sage"     style="background:#2D7D5E" onclick="applyTheme('sage')"     title="Sage"></span>
    <span class="theme-dot"        data-theme="sunset"   style="background:#E05A1B" onclick="applyTheme('sunset')"   title="Sunset"></span>
    <span class="theme-dot"        data-theme="lavender" style="background:#7C3AED" onclick="applyTheme('lavender')" title="Lavender"></span>
  </div>
  <div class="sidebar-sep"></div>
  <div id="show-all" class="active" onclick="selectSheet(null)">All Projects</div>
  <div id="upcoming-btn" onclick="selectUpcoming()">📅 Upcoming</div>
  <div id="notes-btn" onclick="selectView('notes')">📝 Notes</div>
  <div id="collabs-btn" onclick="selectView('collaborators')">👥 Collaborators</div>
  <div id="stats-btn"         onclick="selectView('stats')">📊 Stats</div>
  <div id="procrastinate-btn" onclick="selectView('procrastinate')">🐍 Procrastinate</div>
  <div class="sidebar-sep"></div>
  <div id="sheet-list"></div>
  <div id="sidebar-footer">
    <button id="new-project-btn" onclick="openNewProjectModal()">+ New Project</button>
  </div>
  <div id="sidebar-resize"></div>
</div>

<div id="main">
  <div id="topbar">
    <h2 id="topbar-title">All Projects</h2>
    <div id="filter-area">
      <button class="filter-btn active" data-s="all" onclick="setFilter('all')">All</button>
      <button class="filter-btn" data-s="Pending" onclick="setFilter('Pending')">Pending</button>
      <button class="filter-btn" data-s="In Progress" onclick="setFilter('In Progress')">In Progress</button>
      <button class="filter-btn" data-s="Not Started" onclick="setFilter('Not Started')">Not Started</button>
      <button class="filter-btn" data-s="Completed" onclick="setFilter('Completed')">Completed</button>
    </div>
    <input id="search-input" type="search" placeholder="🔍 Search tasks…" oninput="onSearch()" style="display:none">
    <button id="export-btn" onclick="exportCSV()" style="display:none">⬇ Export</button>
    <button id="refresh-btn" onclick="syncAll()">↻ Sync</button>
    <button id="add-btn" onclick="openAddModal()">+ Add Task</button>
  </div>
  <div id="content"><div class="spinner">Loading tasks…</div></div>
  <div id="bulk-bar">
    <span id="bulk-count"></span>
    <button class="bulk-btn" onclick="bulkMark('Completed')">✅ Complete</button>
    <button class="bulk-btn" onclick="bulkMark('In Progress')">▶ In Progress</button>
    <button class="bulk-btn" onclick="bulkMark('Not Started')">○ Not Started</button>
    <button class="bulk-btn" onclick="bulkMark('Pending')">⏸ Pending</button>
    <button class="bulk-btn danger" onclick="bulkDelete()">🗑 Delete</button>
    <button class="bulk-btn bulk-clear" onclick="clearBulkSelect()">✕ Clear</button>
  </div>
</div>

<!-- Add / Edit Modal -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h3 id="modal-title">Add Task</h3>
    <div class="field">
      <label>Project</label>
      <select id="m-sheet"></select>
    </div>
    <div class="field">
      <label>Task</label>
      <textarea id="m-task" rows="2" placeholder="Task description…"></textarea>
    </div>
    <div class="field">
      <label>Deadline</label>
      <input id="m-deadline" type="date">
    </div>
    <div class="field">
      <label>Hours estimate</label>
      <input id="m-hours" type="text" placeholder="e.g. 2">
    </div>
    <div class="field">
      <label>Status</label>
      <select id="m-status">
        <option>Not Started</option>
        <option>In Progress</option>
        <option>Pending</option>
        <option>Completed</option>
      </select>
    </div>
    <div class="field">
      <label>Assignees</label>
      <div id="m-assignee-list" style="display:flex;flex-wrap:wrap;gap:7px;padding:4px 0;min-height:28px;max-height:88px;overflow-y:auto"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-save" onclick="saveModal()">Save</button>
    </div>
  </div>
</div>

<!-- Note modal -->
<div class="modal-overlay" id="note-modal">
  <div class="modal">
    <h3 id="note-modal-title">Add Note</h3>
    <div class="field">
      <label>Project</label>
      <select id="n-project"></select>
    </div>
    <div class="field">
      <label>Note</label>
      <textarea id="n-text" rows="4" placeholder="Write your note…"></textarea>
    </div>
    <div class="field" style="display:flex;gap:14px">
      <div style="flex:1">
        <label>Importance</label>
        <select id="n-importance">
          <option>High</option><option selected>Medium</option><option>Low</option>
        </select>
      </div>
      <div style="flex:1">
        <label>Purpose</label>
        <select id="n-purpose">
          <option>Design</option><option>Writing</option><option>Analysis</option>
          <option>Planning</option><option>Other</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>Color</label>
      <div class="color-swatches" id="n-color-swatches"></div>
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeNoteModal()">Cancel</button>
      <button class="btn-save" onclick="saveNote()">Save</button>
    </div>
  </div>
</div>

<!-- Collaborator modal -->
<div class="modal-overlay" id="collab-modal">
  <div class="modal" style="max-width:380px">
    <h3>Add Collaborator</h3>
    <div class="field">
      <label>Project</label>
      <select id="c-project"></select>
    </div>
    <div class="field">
      <label>Name</label>
      <input id="c-name" type="text" placeholder="e.g. Alice, Bob, Carol (comma-separated)">
    </div>
    <div class="field">
      <label>Role</label>
      <input id="c-role" type="text" placeholder="e.g. Co-author, RA, Advisor">
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeCollabModal()">Cancel</button>
      <button class="btn-save" onclick="saveCollab()">Add</button>
    </div>
  </div>
</div>

<!-- New project modal -->
<div class="modal-overlay" id="project-modal">
  <div class="modal" style="max-width:360px">
    <h3>New Project</h3>
    <div class="field">
      <label>Project Name</label>
      <input id="p-name" type="text" placeholder="e.g. NeurIPS 2026 Paper">
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="document.getElementById('project-modal').classList.remove('open')">Cancel</button>
      <button class="btn-save" onclick="saveNewProject()">Create</button>
    </div>
  </div>
</div>

<!-- Procrastination timeout overlay -->
<div id="procrastinate-overlay">
  <div class="po-box">
    <div id="po-emoji">⏰</div>
    <div id="po-text">TIME'S UP.</div>
    <div id="po-sub">Get back to work.</div>
    <button class="btn-save" onclick="closeProcrastinateOverlay()" style="min-width:140px">OK, fine 😔</button>
  </div>
</div>

<!-- Status picker -->
<div class="status-select" id="status-picker">
  <div class="status-option" onclick="pickStatus('Not Started')">
    <span class="status-dot" style="background:var(--notstarted-dot)"></span>Not Started
  </div>
  <div class="status-option" onclick="pickStatus('In Progress')">
    <span class="status-dot" style="background:var(--inprogress-dot)"></span>In Progress
  </div>
  <div class="status-option" onclick="pickStatus('Pending')">
    <span class="status-dot" style="background:var(--pending-dot)"></span>Pending
  </div>
  <div class="status-option" onclick="pickStatus('Completed')">
    <span class="status-dot" style="background:var(--completed-dot)"></span>Completed
  </div>
</div>

<script>
let allSheets  = [];
let allTasks   = {};
let allNotes   = [];
let allCollabs = [];
let activeSheet  = null;
let activeFilter = 'all';
let viewMode     = 'tasks';
let editTarget   = null;
let modalMode    = 'add';
let noteEditRow  = null;
let selectedNoteColor = '#FFF9C4';
let searchQuery = '';
const selectedTasks = new Set();  // "sheet::row"
const NOTE_COLORS = ['#FFF9C4','#C8E6C9','#BBDEFB','#F8BBD0','#E1BEE7','#FFE0B2'];

const THEMES = {
  classic:  { '--bg':'#F4F6F8', '--surface':'#FFFFFF', '--sidebar-bg':'#1A1A2E', '--sidebar-text':'#A0AEC0', '--sidebar-active':'#FFFFFF', '--sidebar-hover':'rgba(255,255,255,0.08)', '--border':'#EAEAEA', '--text':'#1A202C', '--text-muted':'#718096', '--accent':'#5A67D8', '--accent-light':'rgba(90,103,216,0.12)' },
  ocean:    { '--bg':'#EFF7FF', '--surface':'#FFFFFF', '--sidebar-bg':'#0F3460', '--sidebar-text':'#90B8D4', '--sidebar-active':'#FFFFFF', '--sidebar-hover':'rgba(255,255,255,0.08)', '--border':'#D9EAF7', '--text':'#0D2137', '--text-muted':'#5B7FA0', '--accent':'#0072CE', '--accent-light':'rgba(0,114,206,0.12)' },
  sage:     { '--bg':'#F0F4F0', '--surface':'#FFFFFF', '--sidebar-bg':'#1C3A2F', '--sidebar-text':'#85B09A', '--sidebar-active':'#FFFFFF', '--sidebar-hover':'rgba(255,255,255,0.08)', '--border':'#D8E9E0', '--text':'#1A2E26', '--text-muted':'#5A7B6A', '--accent':'#2D7D5E', '--accent-light':'rgba(45,125,94,0.12)' },
  sunset:   { '--bg':'#FFF6F0', '--surface':'#FFFFFF', '--sidebar-bg':'#2D1B00', '--sidebar-text':'#C4A385', '--sidebar-active':'#FFFFFF', '--sidebar-hover':'rgba(255,255,255,0.08)', '--border':'#F5DFCF', '--text':'#1E1008', '--text-muted':'#8B6A58', '--accent':'#E05A1B', '--accent-light':'rgba(224,90,27,0.12)' },
  lavender: { '--bg':'#F5F0FF', '--surface':'#FFFFFF', '--sidebar-bg':'#1E1040', '--sidebar-text':'#C4B5E8', '--sidebar-active':'#FFFFFF', '--sidebar-hover':'rgba(255,255,255,0.08)', '--border':'#E5D8FF', '--text':'#1E1030', '--text-muted':'#7B6B99', '--accent':'#7C3AED', '--accent-light':'rgba(124,58,237,0.12)' },
};

function applyTheme(name) {
  const vars = THEMES[name];
  if (!vars) return;
  const root = document.documentElement;
  Object.entries(vars).forEach(([k, v]) => root.style.setProperty(k, v));
  localStorage.setItem('theme', name);
  document.querySelectorAll('.theme-dot').forEach(d =>
    d.classList.toggle('active', d.dataset.theme === name)
  );
}

// ── Init ──────────────────────────────────────────────────────────────────

async function init() {
  applyTheme(localStorage.getItem('theme') || 'classic');
  document.getElementById('content').innerHTML = '<div class="spinner">Loading tasks…</div>';
  const status = await fetch('/api/status').then(r => r.json()).catch(() => ({}));
  if (status.local_mode) {
    document.getElementById('local-mode-badge').style.display = '';
    document.getElementById('refresh-btn').title = 'Reload from local storage';
  }
  await Promise.all([loadSheets(), loadTasks(), loadNotes(), loadCollabs()]);
}

async function loadSheets() {
  const res = await fetch('/api/sheets');
  allSheets = await res.json();
  renderSidebar();
}

async function loadTasks(silent = false) {
  if (!silent) {
    document.getElementById('content').innerHTML = '<div class="spinner">Loading…</div>';
  }
  const res = await fetch('/api/tasks');  // always fetch all; filter client-side
  allTasks = await res.json();
  renderContent();
}

async function loadNotes() {
  const res = await fetch('/api/notes');
  allNotes = await res.json();
}

async function loadCollabs() {
  const res = await fetch('/api/collaborators');
  allCollabs = await res.json();
}

async function syncAll() {
  const btn = document.getElementById('refresh-btn');
  btn.textContent = '↻ Syncing…';
  btn.disabled = true;
  try {
    await fetch('/api/sync', { method: 'POST' });
    await Promise.all([loadSheets(), loadTasks(true), loadNotes(), loadCollabs()]);
    renderContent();
  } finally {
    btn.textContent = '↻ Sync';
    btn.disabled = false;
  }
}

// ── Sidebar ───────────────────────────────────────────────────────────────

function setSidebarActive(id) {
  ['show-all','upcoming-btn','notes-btn','collabs-btn','stats-btn','procrastinate-btn'].forEach(i =>
    document.getElementById(i).className = (i === id ? 'active' : ''));
  document.querySelectorAll('.sheet-item').forEach(el => el.classList.remove('active'));
}

function updateTopbar() {
  const isTaskView = viewMode === 'tasks' || viewMode === 'upcoming';
  document.getElementById('filter-area').style.display = isTaskView ? '' : 'none';
  const searchEl = document.getElementById('search-input');
  const exportEl = document.getElementById('export-btn');
  if (searchEl) searchEl.style.display = viewMode === 'tasks' ? '' : 'none';
  if (exportEl) exportEl.style.display = viewMode === 'tasks' ? '' : 'none';
  const btn = document.getElementById('add-btn');
  if (viewMode === 'procrastinate' || viewMode === 'stats') {
    btn.style.display = 'none';
  } else {
    btn.style.display = '';
    if (viewMode === 'notes') {
      btn.textContent = '+ Add Note';
      btn.onclick = () => openNoteModal(null);
    } else if (viewMode === 'collaborators') {
      btn.textContent = '+ Add Collaborator';
      btn.onclick = () => openCollabModal();
    } else {
      btn.textContent = '+ Add Task';
      btn.onclick = () => openAddModal();
    }
  }
}

function renderSidebar() {
  const list = document.getElementById('sheet-list');
  list.innerHTML = '';
  let totalActive = 0;
  allSheets.forEach(s => {
    totalActive += s.active;
    const div = document.createElement('div');
    div.className = 'sheet-item' + (activeSheet === s.name ? ' active' : '');
    div.innerHTML = `<span>${s.name}</span><span class="badge">${s.active}</span>`;
    div.onclick = () => selectSheet(s.name);
    list.appendChild(div);
  });
  document.getElementById('total-count').textContent = `${totalActive} active tasks`;
}

function selectSheet(name) {
  clearBulkSelect();
  searchQuery = '';
  const si = document.getElementById('search-input'); if (si) si.value = '';
  viewMode = 'tasks';
  activeSheet = name;
  document.getElementById('topbar-title').textContent = name || 'All Projects';
  setSidebarActive(name ? null : 'show-all');
  if (!name) document.getElementById('show-all').className = 'active';
  else document.querySelectorAll('.sheet-item').forEach(el => {
    el.classList.toggle('active', el.querySelector('span').textContent === name);
  });
  updateTopbar();
  renderContent();  // use in-memory data, no network fetch
}

function selectUpcoming() {
  clearBulkSelect();
  viewMode = 'upcoming';
  activeSheet = null;
  document.getElementById('topbar-title').textContent = 'Upcoming';
  setSidebarActive('upcoming-btn');
  updateTopbar();
  renderContent();  // use in-memory data, no network fetch
}

function selectView(mode) {
  clearBulkSelect();
  searchQuery = '';
  const si = document.getElementById('search-input'); if (si) si.value = '';
  viewMode = mode;
  activeSheet = null;
  document.getElementById('topbar-title').textContent =
    { notes:'Notes', collaborators:'Collaborators', procrastinate:'🐍 Procrastinate', stats:'📊 Stats' }[mode] || mode;
  const sid = { notes:'notes-btn', collaborators:'collabs-btn', procrastinate:'procrastinate-btn', stats:'stats-btn' };
  setSidebarActive(sid[mode] || null);
  updateTopbar();
  renderContent();
}

function renderContent() {
  if (viewMode !== 'procrastinate') cleanupProcrastinate();
  if (viewMode === 'upcoming')           renderUpcoming();
  else if (viewMode === 'notes')         renderNotes();
  else if (viewMode === 'collaborators') renderCollaborators();
  else if (viewMode === 'procrastinate') renderProcrastinate();
  else if (viewMode === 'stats')         renderStats();
  else                                   renderTasks();
}

// ── Tasks ─────────────────────────────────────────────────────────────────

function noteProjectCard(n) {
  const preview = n.note.length > 130 ? n.note.slice(0, 130) + '…' : n.note;
  const dateStr = (n.modified && n.modified !== n.created) ? n.modified : n.created;
  return `<div class="project-note-card" style="border-left-color:${escHtml(n.color || '#CBD5E0')}">
    <div class="project-note-meta">
      <span class="note-badge imp-${escHtml(n.importance)}">${escHtml(n.importance)}</span>
      <span class="note-badge pur-${escHtml(n.purpose)}">${escHtml(n.purpose)}</span>
      ${dateStr ? `<span class="project-note-date">${escHtml(dateStr)}</span>` : ''}
    </div>
    <div class="project-note-text">${escHtml(preview)}</div>
    <div class="project-note-actions">
      <button class="note-action-btn" onclick="openNoteModal(${n.row})">✏ Edit</button>
      <button class="note-action-btn note-delete-btn" onclick="deleteNote(${n.row})">✕ Delete</button>
    </div>
  </div>`;
}

function projectExtras(sheet) {
  const chips = allCollabs
    .filter(c => c.project === sheet)
    .map(c => `<span class="collab-chip">👤 ${escHtml(c.name)}${c.role ? `<span class="chip-role"> · ${escHtml(c.role)}</span>` : ''}</span>`)
    .join('');

  const notes = allNotes
    .filter(n => n.project === sheet)
    .sort((a, b) => {
      const da = a.modified || a.created || '';
      const db = b.modified || b.created || '';
      return db.localeCompare(da);
    });

  let notesHtml = '';
  if (notes.length > 0) {
    if (activeSheet) {
      // single-project view: show full cards (newest first, max 3)
      const show = notes.slice(0, 3);
      const more = notes.length - show.length;
      const cards = show.map(noteProjectCard).join('');
      notesHtml = `<div class="project-notes-section">
        <div class="collab-chip-label" style="margin-bottom:6px">Notes</div>
        <div class="project-notes-grid">${cards}</div>
        ${more > 0 ? `<button class="more-notes-btn" onclick="selectView('notes')">+ ${more} more note${more > 1 ? 's' : ''} →</button>` : ''}
      </div>`;
    } else {
      // all-projects view: just a count pill linking to notes view
      notesHtml = `<button class="notes-count-pill" onclick="selectView('notes')">📝 ${notes.length} note${notes.length !== 1 ? 's' : ''}</button>`;
    }
  }

  if (!chips && !notesHtml) return '';
  return `<div class="project-extras">
    ${chips ? `<div class="collab-chips"><span class="collab-chip-label">Team</span>${chips}</div>` : ''}
    ${notesHtml}
  </div>`;
}

function renderTasks() {
  const content = document.getElementById('content');
  const scoped = activeSheet
    ? (allTasks[activeSheet] ? { [activeSheet]: allTasks[activeSheet] } : {})
    : allTasks;

  let filtered = filterTasks(scoped);
  if (searchQuery) {
    const q = searchQuery;
    const out = {};
    Object.entries(filtered).forEach(([sheet, tasks]) => {
      const f = tasks.filter(t =>
        t.task.toLowerCase().includes(q) || t.deadline.toLowerCase().includes(q) ||
        (t.assignee||'').toLowerCase().includes(q) || t.status.toLowerCase().includes(q)
      );
      if (f.length) out[sheet] = f;
    });
    filtered = out;
  }

  const sheets = Object.keys(filtered);
  const gardenHtml = (!activeSheet && !searchQuery) ? renderGarden() : '';

  if (!sheets.length) {
    content.innerHTML = gardenHtml +
      `<div class="empty">${searchQuery ? `No tasks match &ldquo;${escHtml(searchQuery)}&rdquo;.` : 'No tasks match the current filter.'}</div>`;
    return;
  }

  const taskHtml = sheets.map(sheet => {
    const tasks = filtered[sheet];
    const allSheetTasks = allTasks[sheet] || [];
    const doneCount = allSheetTasks.filter(t => t.status === 'Completed').length;
    const rows = tasks.map(t => taskRow(sheet, t)).join('');
    const extras = projectExtras(sheet);
    return `<div class="section">
      <div class="section-header">
        ${flowerSVG(sheet, allSheetTasks, 44)}
        <span class="section-title">${escHtml(sheet)}</span>
        <span class="section-done">${doneCount}&thinsp;/&thinsp;${allSheetTasks.length}</span>
        <button class="section-del-btn" onclick="deleteProject('${jsStr(sheet)}')" title="Delete project">🗑</button>
      </div>
      <table class="task-table"><tbody>${rows}</tbody></table>
      ${extras}
    </div>`;
  }).join('');

  content.innerHTML = gardenHtml + taskHtml;
}

function parseDeadline(s) {
  if (!s || !s.trim()) return null;
  const cleaned = s.trim().replace(/(\d+)(st|nd|rd|th)\b/gi, '$1');
  const d = new Date(cleaned);
  return isNaN(d.getTime()) ? null : d;
}

function deadlinePillClass(d) {
  if (!d) return 'deadline-later';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const week  = new Date(today); week.setDate(today.getDate() + 7);
  const dt = new Date(d); dt.setHours(0, 0, 0, 0);
  if (dt < today)  return 'deadline-overdue';
  if (dt.getTime() === today.getTime()) return 'deadline-today';
  if (dt <= week)  return 'deadline-week';
  return 'deadline-normal';
}

function renderUpcoming() {
  const content = document.getElementById('content');
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const week  = new Date(today); week.setDate(today.getDate() + 7);
  const month = new Date(today); month.setDate(today.getDate() + 30);

  const groups = { overdue: [], today: [], week: [], month: [], later: [], none: [] };

  Object.entries(allTasks).forEach(([sheet, tasks]) => {
    tasks.forEach(t => {
      if (t.status === 'Completed') return;
      const item = { ...t, sheet };
      const d = parseDeadline(t.deadline);
      if (!d) { groups.none.push(item); return; }
      const dt = new Date(d); dt.setHours(0, 0, 0, 0);
      if (dt < today)                       groups.overdue.push(item);
      else if (dt.getTime() === today.getTime()) groups.today.push(item);
      else if (dt <= week)                   groups.week.push(item);
      else if (dt <= month)                  groups.month.push(item);
      else                                   groups.later.push(item);
    });
  });

  const byDate = (a, b) => (parseDeadline(a.deadline) || 0) - (parseDeadline(b.deadline) || 0);

  const sections = [
    { key: 'overdue', label: 'Overdue',      color: 'var(--overdue-color)' },
    { key: 'today',   label: 'Today',         color: 'var(--today-color)'  },
    { key: 'week',    label: 'This Week',     color: 'var(--week-color)'   },
    { key: 'month',   label: 'This Month',    color: 'var(--month-color)'  },
    { key: 'later',   label: 'Later',         color: 'var(--later-color)'  },
    { key: 'none',    label: 'No Deadline',   color: 'var(--later-color)'  },
  ];

  const html = sections
    .filter(s => groups[s.key].length)
    .map(s => {
      const items = s.key === 'none' ? groups[s.key] : groups[s.key].sort(byDate);
      const rows = items.map(t => upcomingRow(t)).join('');
      return `<div class="section">
        <div class="section-title" style="color:${s.color}">${s.label} &mdash; ${items.length} task${items.length !== 1 ? 's' : ''}</div>
        <table class="task-table"><tbody>${rows}</tbody></table>
      </div>`;
    }).join('');

  content.innerHTML = html || '<div class="empty">No upcoming tasks.</div>';
}

function upcomingRow(t) {
  const pillClass = deadlinePillClass(parseDeadline(t.deadline));
  const deadlineHtml = t.deadline
    ? `<span class="deadline-pill ${pillClass}">${escHtml(t.deadline)}</span>`
    : '';
  const meta = [t.sheet, t.hours ? `⏱ ${t.hours}h` : ''].filter(Boolean).join('  ·  ');
  return `<tr class="task-row">
    <td style="width:100%">
      <div class="task-text">${escHtml(t.task)}</div>
      ${meta ? `<div class="task-meta">${escHtml(meta)}</div>` : ''}
    </td>
    <td style="padding-right:8px">${deadlineHtml}</td>
    <td>
      <span class="status-badge status-${escHtml(t.status)}"
            onclick="openStatusPicker(event,'${jsStr(t.sheet)}',${t.row},'${jsStr(t.status)}')">
        ${escHtml(t.status)}
      </span>
    </td>
    <td style="white-space:nowrap">
      <button class="icon-btn" title="Edit" onclick="openEditModal('${jsStr(t.sheet)}',${t.row})">✏️</button>
      <button class="icon-btn" title="Delete" onclick="deleteTask('${jsStr(t.sheet)}',${t.row})">🗑</button>
    </td>
  </tr>`;
}

function filterTasks(tasks) {
  if (activeFilter === 'all') return tasks;
  const out = {};
  Object.entries(tasks).forEach(([sheet, list]) => {
    const f = list.filter(t => t.status === activeFilter);
    if (f.length) out[sheet] = f;
  });
  return out;
}

function taskRow(sheet, t) {
  const metaParts = [
    t.deadline ? `📅 ${t.deadline}` : '',
    t.hours    ? `⏱ ${t.hours}h`   : '',
    t.assignee ? `👤 ${t.assignee}` : '',
    t.status === 'Completed' && t.completed_date ? `✅ ${t.completed_date}` : '',
  ].filter(Boolean);
  const key = `${sheet}::${t.row}`;
  const checked = selectedTasks.has(key) ? 'checked' : '';
  return `<tr class="task-row${checked ? ' selected' : ''}">
    <td style="width:20px;padding:0 6px 0 14px">
      <input type="checkbox" class="task-check" ${checked}
        data-sheet="${escHtml(sheet)}" data-row="${t.row}" onchange="onTaskSelect()">
    </td>
    <td style="width:100%">
      <div class="task-text">${escHtml(t.task)}</div>
      ${metaParts.length ? `<div class="task-meta">${metaParts.join('  ')}</div>` : ''}
    </td>
    <td>
      <span class="status-badge status-${escHtml(t.status)}"
            onclick="openStatusPicker(event,'${jsStr(sheet)}',${t.row},'${jsStr(t.status)}')">
        ${escHtml(t.status)}
      </span>
    </td>
    <td style="white-space:nowrap">
      <button class="icon-btn" title="Edit" onclick="openEditModal('${jsStr(sheet)}',${t.row})">✏️</button>
      <button class="icon-btn" title="Delete" onclick="deleteTask('${jsStr(sheet)}',${t.row})">🗑</button>
    </td>
  </tr>`;
}

function setFilter(f) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.s === f);
  });
  renderTasks();
}

// ── Status picker ─────────────────────────────────────────────────────────

function openStatusPicker(e, sheet, row, current) {
  e.stopPropagation();
  editTarget = { sheet, row };
  const picker = document.getElementById('status-picker');
  picker.classList.add('open');
  const ph = picker.offsetHeight;
  const pw = picker.offsetWidth;
  const top = (e.clientY + 8 + ph > window.innerHeight - 8) ? e.clientY - ph - 8 : e.clientY + 8;
  picker.style.top  = Math.max(8, top) + 'px';
  picker.style.left = Math.min(e.clientX, window.innerWidth - pw - 8) + 'px';
}

async function pickStatus(status) {
  document.getElementById('status-picker').classList.remove('open');
  if (!editTarget) return;
  const { sheet, row } = editTarget;
  await fetch(`/api/tasks/${encodeURIComponent(sheet)}/${row}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status })
  });
  await Promise.all([loadSheets(), loadTasks(true)]);
}

document.addEventListener('click', () => {
  document.getElementById('status-picker').classList.remove('open');
});

// ── Modal ─────────────────────────────────────────────────────────────────

function populateSheetSelect(selected) {
  const sel = document.getElementById('m-sheet');
  sel.innerHTML = allSheets.map(s =>
    `<option ${s.name === selected ? 'selected' : ''}>${s.name}</option>`
  ).join('');
  populateAssigneeSelect(selected);
}

function populateAssigneeSelect(project, current = '') {
  const container = document.getElementById('m-assignee-list');
  const projectCollabs = allCollabs.filter(c => c.project === project);
  const currentList = current.split(',').map(s => s.trim()).filter(Boolean);
  if (!projectCollabs.length) {
    container.innerHTML = '<span style="font-size:12px;color:var(--text-muted)">No collaborators on this project yet</span>';
    return;
  }
  container.innerHTML = projectCollabs.map(c =>
    `<label style="display:inline-flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;padding:3px 10px;background:var(--bg);border-radius:20px;border:1.5px solid var(--border);white-space:nowrap">
      <input type="checkbox" class="assignee-cb" value="${escHtml(c.name)}"
        ${currentList.includes(c.name) ? 'checked' : ''}
        style="cursor:pointer;accent-color:var(--accent)">
      ${escHtml(c.name)}
    </label>`
  ).join('');
}

function openAddModal() {
  modalMode = 'add';
  editTarget = null;
  document.getElementById('modal-title').textContent = 'Add Task';
  const sheet = activeSheet || allSheets[0]?.name;
  populateSheetSelect(sheet);
  document.getElementById('m-task').value = '';
  document.getElementById('m-deadline').value = '';
  document.getElementById('m-hours').value = '';
  document.getElementById('m-status').value = 'Not Started';
  document.getElementById('m-sheet').onchange = e => populateAssigneeSelect(e.target.value);
  document.getElementById('modal').classList.add('open');
}

function openEditModal(sheet, row) {
  const task = allTasks[sheet]?.find(t => t.row === row);
  if (!task) return;
  modalMode = 'edit';
  editTarget = { sheet, row };
  document.getElementById('modal-title').textContent = 'Edit Task';
  populateSheetSelect(sheet);
  populateAssigneeSelect(sheet, task.assignee || '');
  document.getElementById('m-sheet').disabled = true;
  document.getElementById('m-task').value = task.task;
  document.getElementById('m-deadline').value = toDateInputValue(task.deadline);
  document.getElementById('m-hours').value = task.hours;
  document.getElementById('m-status').value = task.status;
  document.getElementById('modal').classList.add('open');
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('m-sheet').disabled = false;
}

async function saveModal() {
  const sheet    = document.getElementById('m-sheet').value;
  const task     = document.getElementById('m-task').value.trim();
  const deadline = fromDateInput(document.getElementById('m-deadline').value);
  const hours    = document.getElementById('m-hours').value.trim();
  const status   = document.getElementById('m-status').value;
  const assignee = [...document.querySelectorAll('#m-assignee-list .assignee-cb:checked')]
    .map(cb => cb.value).join(', ');

  if (!task) { alert('Task description is required.'); return; }

  if (modalMode === 'add') {
    await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, task, deadline, hours, status, assignee })
    });
  } else {
    const { row } = editTarget;
    await fetch(`/api/tasks/${encodeURIComponent(sheet)}/${row}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task, deadline, hours, status, assignee })
    });
  }
  closeModal();
  await Promise.all([loadSheets(), loadTasks(true)]);
}

async function deleteTask(sheet, row) {
  if (!confirm('Delete this task?')) return;
  await fetch(`/api/tasks/${encodeURIComponent(sheet)}/${row}`, { method: 'DELETE' });
  await Promise.all([loadSheets(), loadTasks(true)]);
}

// ── Notes view ────────────────────────────────────────────────────────────

function renderNotes() {
  const content = document.getElementById('content');
  const projectFilter = document.getElementById('n-filter-project')?.value || '';
  const impFilter  = document.getElementById('n-filter-imp')?.value  || '';
  const purFilter  = document.getElementById('n-filter-pur')?.value  || '';

  let notes = allNotes;
  if (projectFilter) notes = notes.filter(n => n.project === projectFilter);
  if (impFilter)     notes = notes.filter(n => n.importance === impFilter);
  if (purFilter)     notes = notes.filter(n => n.purpose === purFilter);

  const projectOpts = ['', ...allSheets.map(s => s.name)]
    .map(p => `<option value="${escHtml(p)}" ${p===projectFilter?'selected':''}>${p||'All Projects'}</option>`).join('');
  const impOpts = ['','High','Medium','Low']
    .map(v => `<option value="${v}" ${v===impFilter?'selected':''}>${v||'All Importance'}</option>`).join('');
  const purOpts = ['','Design','Writing','Analysis','Planning','Other']
    .map(v => `<option value="${v}" ${v===purFilter?'selected':''}>${v||'All Purposes'}</option>`).join('');

  const toolbar = `<div class="notes-toolbar">
    <select id="n-filter-project" onchange="renderNotes()">${projectOpts}</select>
    <select id="n-filter-imp"     onchange="renderNotes()">${impOpts}</select>
    <select id="n-filter-pur"     onchange="renderNotes()">${purOpts}</select>
  </div>`;

  if (!notes.length) {
    content.innerHTML = toolbar + '<div class="empty">No notes yet. Click "+ Add Note" to create one.</div>';
    return;
  }

  const cards = notes.map(n => {
    const borderColor = n.color || '#CBD5E0';
    const created  = n.created  ? `📅 ${n.created}`  : '';
    const modified = n.modified && n.modified !== n.created ? ` · edited ${n.modified}` : '';
    return `<div class="note-card" style="border-left-color:${escHtml(borderColor)}">
      <div class="note-header">
        <span class="note-project">${escHtml(n.project)}</span>
        <span class="note-badge imp-${escHtml(n.importance)}">${escHtml(n.importance)}</span>
        <span class="note-badge pur-${escHtml(n.purpose)}">${escHtml(n.purpose)}</span>
      </div>
      <div class="note-text">${escHtml(n.note)}</div>
      <div class="note-footer">
        <span class="note-dates">${created}${modified}</span>
        <div class="note-actions-inline">
          <button class="note-action-btn" onclick="openNoteModal(${n.row})">✏ Edit</button>
          <button class="note-action-btn note-delete-btn" onclick="deleteNote(${n.row})">✕ Delete</button>
        </div>
      </div>
    </div>`;
  }).join('');

  content.innerHTML = toolbar + `<div class="notes-grid">${cards}</div>`;
}

function openNoteModal(row = null) {
  if (typeof row !== 'number') row = null;  // guard against click-event being passed
  noteEditRow = row;
  selectedNoteColor = NOTE_COLORS[0];
  document.getElementById('note-modal-title').textContent = row ? 'Edit Note' : 'Add Note';

  const npSel = document.getElementById('n-project');
  npSel.innerHTML = allSheets.map(s =>
    `<option>${escHtml(s.name)}</option>`
  ).join('');

  if (row) {
    const n = allNotes.find(x => x.row === row);
    if (n) {
      npSel.value = n.project;
      document.getElementById('n-text').value = n.note;
      document.getElementById('n-importance').value = n.importance;
      document.getElementById('n-purpose').value = n.purpose;
      selectedNoteColor = n.color || NOTE_COLORS[0];
    }
  } else {
    if (activeSheet) npSel.value = activeSheet;
    document.getElementById('n-text').value = '';
    document.getElementById('n-importance').value = 'Medium';
    document.getElementById('n-purpose').value = 'Other';
  }
  renderColorSwatches();
  document.getElementById('note-modal').classList.add('open');
}

function renderColorSwatches() {
  const container = document.getElementById('n-color-swatches');
  container.innerHTML = NOTE_COLORS.map(c =>
    `<div class="color-swatch ${c===selectedNoteColor?'selected':''}"
          style="background:${c}"
          onclick="selectNoteColor('${c}')"></div>`
  ).join('');
}

function selectNoteColor(c) {
  selectedNoteColor = c;
  renderColorSwatches();
}

function closeNoteModal() {
  document.getElementById('note-modal').classList.remove('open');
}

async function saveNote() {
  const project    = document.getElementById('n-project').value;
  const note       = document.getElementById('n-text').value.trim();
  const importance = document.getElementById('n-importance').value;
  const purpose    = document.getElementById('n-purpose').value;
  if (!note) { alert('Note text is required.'); return; }
  const body = { project, note, importance, purpose, color: selectedNoteColor };
  const url    = noteEditRow ? `/api/notes/${noteEditRow}` : '/api/notes';
  const method = noteEditRow ? 'PUT' : 'POST';
  const res = await fetch(url, {
    method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  if (!res.ok) { alert('Failed to save note. Check the server log.'); return; }
  closeNoteModal();
  await loadNotes();
  renderContent();
}

async function deleteNote(row) {
  if (!confirm('Delete this note?')) return;
  await fetch(`/api/notes/${row}`, { method: 'DELETE' });
  await loadNotes();
  renderContent();
}

// ── Collaborators view ────────────────────────────────────────────────────

function renderCollaborators() {
  const content = document.getElementById('content');
  if (!allCollabs.length && !allSheets.length) {
    content.innerHTML = '<div class="empty">No collaborators yet.</div>';
    return;
  }

  const byProject = {};
  allSheets.forEach(s => { byProject[s.name] = []; });
  allCollabs.forEach(c => {
    if (!byProject[c.project]) byProject[c.project] = [];
    byProject[c.project].push(c);
  });

  const html = Object.entries(byProject).map(([project, collabs]) => {
    const rows = collabs.map(c => {
      const inAssignees = t => (t.assignee||'').split(',').map(s=>s.trim()).includes(c.name);
      const taskNames = Object.entries(allTasks)
        .filter(([s]) => s === project)
        .flatMap(([, tasks]) => tasks.filter(t => inAssignees(t) && t.status !== 'Completed'))
        .map(t => t.task.length > 40 ? t.task.slice(0,40)+'…' : t.task);
      return `<tr class="collab-row">
        <td>
          <div class="collab-name">${escHtml(c.name)}</div>
          ${c.role ? `<div class="collab-role">${escHtml(c.role)}</div>` : ''}
          ${taskNames.length ? `<div class="collab-tasks">📋 ${taskNames.map(escHtml).join(', ')}</div>` : ''}
        </td>
        <td style="white-space:nowrap">
          <button class="note-action-btn" onclick="deleteCollab(${c.row})">✕ Remove</button>
        </td>
      </tr>`;
    }).join('');

    return `<div class="collab-section">
      <div class="section-title">${escHtml(project)}</div>
      <table class="collab-table"><tbody>
        ${rows || `<tr><td style="color:var(--text-muted);font-size:13px;padding:12px 14px">No collaborators yet.</td></tr>`}
      </tbody></table>
    </div>`;
  }).join('');

  content.innerHTML = html || '<div class="empty">No projects found.</div>';
}

function openCollabModal() {
  const sel = document.getElementById('c-project');
  sel.innerHTML = allSheets.map(s => `<option>${escHtml(s.name)}</option>`).join('');
  if (activeSheet) sel.value = activeSheet;
  document.getElementById('c-name').value = '';
  document.getElementById('c-role').value = '';
  document.getElementById('collab-modal').classList.add('open');
}

function closeCollabModal() {
  document.getElementById('collab-modal').classList.remove('open');
}

async function saveCollab() {
  const project = document.getElementById('c-project').value;
  const names   = document.getElementById('c-name').value.split(',').map(n => n.trim()).filter(Boolean);
  const role    = document.getElementById('c-role').value.trim();
  if (!names.length) { alert('Name is required.'); return; }
  await fetch('/api/collaborators', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ project, names, role })
  });
  closeCollabModal();
  await loadCollabs();
  renderContent();
}

async function deleteCollab(row) {
  if (!confirm('Remove this collaborator?')) return;
  await fetch(`/api/collaborators/${row}`, { method: 'DELETE' });
  await loadCollabs();
  renderContent();
}

// ── New project ───────────────────────────────────────────────────────────

function openNewProjectModal() {
  document.getElementById('p-name').value = '';
  document.getElementById('project-modal').classList.add('open');
}

async function saveNewProject() {
  const name = document.getElementById('p-name').value.trim();
  if (!name) { alert('Project name is required.'); return; }
  const res = await fetch('/api/projects', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ name })
  });
  const data = await res.json();
  if (!res.ok) { alert(data.error || 'Failed to create project.'); return; }
  document.getElementById('project-modal').classList.remove('open');
  await Promise.all([loadSheets(), loadTasks(true)]);
  selectSheet(name);
}

// ── Flower engine ────────────────────────────────────────────────────────

function hashStr(s) {
  let h = 0;
  for (const c of s) h = (Math.imul(h, 31) + c.charCodeAt(0)) | 0;
  return Math.abs(h);
}

const FLOWER_DEFS = [
  { center:'#D97706', fill:'#FBBF24', empty:'#FEF9C3', stroke:'#F59E0B', shape:'ellipse'  },
  { center:'#BE185D', fill:'#FBCFE8', empty:'#FFF0F5', stroke:'#EC4899', shape:'round'    },
  { center:'#5B21B6', fill:'#C4B5FD', empty:'#F3EEFF', stroke:'#7C3AED', shape:'teardrop' },
  { center:'#065F46', fill:'#6EE7B7', empty:'#ECFDF5', stroke:'#10B981', shape:'thin'     },
  { center:'#9B1C1C', fill:'#FCA5A5', empty:'#FFF5F5', stroke:'#EF4444', shape:'wide'     },
  { center:'#1E40AF', fill:'#93C5FD', empty:'#EFF6FF', stroke:'#3B82F6', shape:'diamond'  },
];

function _petalEl(shape, len, w, sd, fill, stroke) {
  const e = sd + len, mid = sd + len * 0.5;
  const f = v => v.toFixed(1);
  switch (shape) {
    case 'ellipse':
      return `<ellipse cx="0" cy="${f(-mid)}" rx="${f(w)}" ry="${f(len*0.5)}" fill="${fill}" stroke="${stroke}" stroke-width="0.7"/>`;
    case 'round':
      return `<circle cx="0" cy="${f(-mid)}" r="${f(len*0.52)}" fill="${fill}" stroke="${stroke}" stroke-width="0.7"/>`;
    case 'teardrop':
      return `<path d="M 0 ${f(-sd)} C ${f(w)} ${f(-(sd+len*0.28))} ${f(w*.65)} ${f(-(sd+len*0.78))} 0 ${f(-e)} C ${f(-w*.65)} ${f(-(sd+len*0.78))} ${f(-w)} ${f(-(sd+len*0.28))} 0 ${f(-sd)} Z" fill="${fill}" stroke="${stroke}" stroke-width="0.7"/>`;
    case 'thin':
      return `<path d="M 0 ${f(-sd)} C ${f(w)} ${f(-(sd+len*0.38))} ${f(w*.6)} ${f(-(sd+len*0.75))} 0 ${f(-e)} C ${f(-w*.6)} ${f(-(sd+len*0.75))} ${f(-w)} ${f(-(sd+len*0.38))} 0 ${f(-sd)} Z" fill="${fill}" stroke="${stroke}" stroke-width="0.7"/>`;
    case 'wide':
      return `<path d="M 0 ${f(-sd)} C ${f(w*1.3)} ${f(-(sd+len*0.18))} ${f(w*1.05)} ${f(-(sd+len*0.72))} 0 ${f(-e)} C ${f(-w*1.05)} ${f(-(sd+len*0.72))} ${f(-w*1.3)} ${f(-(sd+len*0.18))} 0 ${f(-sd)} Z" fill="${fill}" stroke="${stroke}" stroke-width="0.7"/>`;
    case 'diamond':
      return `<path d="M 0 ${f(-sd)} L ${f(w)} ${f(-mid)} L 0 ${f(-e)} L ${f(-w)} ${f(-mid)} Z" fill="${fill}" stroke="${stroke}" stroke-width="0.7"/>`;
    default: return '';
  }
}

function flowerSVG(projectName, tasks, size) {
  size = size || 84;
  const type = FLOWER_DEFS[hashStr(projectName) % FLOWER_DEFS.length];
  const N  = tasks.length;
  const cx = size / 2, cy = size / 2;
  const R  = size * 0.44;
  const cR = size * 0.10;
  const SD = cR + size * 0.03;
  const len = R - SD;
  // petal width: as many petals as needed without overlap
  const arcW = N > 0 ? Math.PI * (SD + len * 0.5) / N * 0.78 : len * 0.32;
  const w    = Math.max(size * 0.027, Math.min(len * 0.46, arcW));

  let petals = '';
  for (let i = 0; i < N; i++) {
    const angle = (360 / N) * i - 90;
    const done  = tasks[i]?.status === 'Completed';
    petals += `<g transform="translate(${cx},${cy}) rotate(${angle.toFixed(1)})">` +
      _petalEl(type.shape, len, w, SD, done ? type.fill : type.empty, done ? type.stroke : '#D1D5DB') +
      `</g>`;
  }

  const allDone = N > 0 && tasks.every(t => t.status === 'Completed');
  const centerFill = allDone ? type.fill : type.center;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg">
    ${N === 0 ? `<circle cx="${cx}" cy="${cy}" r="${(cR*1.7).toFixed(1)}" fill="#E5E7EB" stroke="#D1D5DB" stroke-width="0.8"/>` : petals}
    <circle cx="${cx}" cy="${cy}" r="${cR.toFixed(1)}" fill="${centerFill}" stroke="rgba(255,255,255,0.5)" stroke-width="1"/>
    ${allDone ? `<text x="${cx}" y="${(cy+cR*0.42).toFixed(1)}" text-anchor="middle" font-size="${(cR*1.4).toFixed(0)}" fill="white">✓</text>` : ''}
  </svg>`;
}

// ── Garden & delete project ───────────────────────────────────────────────

function renderGarden() {
  if (!allSheets.length) return '';
  const cards = allSheets.map(s => {
    const tasks = allTasks[s.name] || [];
    const done  = tasks.filter(t => t.status === 'Completed').length;
    return `<div class="garden-card" onclick="selectSheet('${jsStr(s.name)}')">
      <button class="garden-delete" title="Delete project"
        onclick="event.stopPropagation();deleteProject('${jsStr(s.name)}')">×</button>
      ${flowerSVG(s.name, tasks, 78)}
      <div class="garden-name">${escHtml(s.name)}</div>
      <div class="garden-progress">${done}&thinsp;/&thinsp;${tasks.length} done</div>
    </div>`;
  }).join('');
  return `<div class="garden-grid">${cards}</div>`;
}

async function deleteProject(name) {
  if (!confirm(`Permanently delete project "${name}" and all its tasks?`)) return;
  const res = await fetch(`/api/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
  if (!res.ok) { alert('Failed to delete project.'); return; }
  await Promise.all([loadSheets(), loadTasks(true)]);
  if (activeSheet === name) selectSheet(null);
  else renderContent();
}

// ── Stats view ────────────────────────────────────────────────────────────

function renderStats() {
  const content = document.getElementById('content');
  const flat  = Object.values(allTasks).flat();
  const total = flat.length;
  const done  = flat.filter(t => t.status === 'Completed').length;
  const pct   = total > 0 ? Math.round(done / total * 100) : 0;
  const hours = flat.reduce((s,t) => s + (parseFloat(t.hours)||0), 0);
  const hDone = flat.filter(t => t.status==='Completed').reduce((s,t) => s+(parseFloat(t.hours)||0), 0);
  const SC = { 'Completed':'var(--completed-dot)', 'In Progress':'var(--inprogress-dot)', 'Pending':'var(--pending-dot)', 'Not Started':'var(--notstarted-dot)' };

  const topCards = `
    <div class="stat-card">
      <div class="stat-label">Overall Progress</div>
      <div class="stat-value">${pct}%</div>
      <div class="stat-sub">${done} of ${total} tasks complete</div>
      <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Hours Logged</div>
      <div class="stat-value">${hours.toFixed(0)}<span style="font-size:15px;font-weight:400">h</span></div>
      <div class="stat-sub">${hDone.toFixed(0)}h in completed work</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Notes</div>
      <div class="stat-value">${allNotes.length}</div>
      <div class="stat-sub">across ${allSheets.length} project${allSheets.length!==1?'s':''}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Collaborators</div>
      <div class="stat-value">${allCollabs.length}</div>
      <div class="stat-sub">${[...new Set(allCollabs.map(c=>c.name))].length} unique people</div>
    </div>`;

  const statusCards = ['Completed','In Progress','Pending','Not Started'].map(s => {
    const n = flat.filter(t => t.status===s).length;
    const p = total > 0 ? Math.round(n/total*100) : 0;
    return `<div class="stat-card">
      <div class="stat-label" style="color:${SC[s]}">${s}</div>
      <div class="stat-value" style="color:${SC[s]}">${n}</div>
      <div class="stat-sub">${p}% of all tasks</div>
      <div class="progress-bar"><div class="progress-fill" style="width:${p}%;background:${SC[s]}"></div></div>
    </div>`;
  }).join('');

  const projectRows = Object.entries(allTasks).map(([name, tasks]) => {
    const d = tasks.filter(t => t.status==='Completed').length;
    const p = tasks.length > 0 ? Math.round(d/tasks.length*100) : 0;
    return `<div class="proj-stat-row" onclick="selectSheet('${jsStr(name)}')">
      ${flowerSVG(name, tasks, 30)}
      <div class="proj-stat-name">${escHtml(name)}</div>
      <div class="proj-stat-bar"><div class="progress-bar"><div class="progress-fill" style="width:${p}%"></div></div></div>
      <div class="proj-stat-pct">${d}&thinsp;/&thinsp;${tasks.length}</div>
    </div>`;
  }).join('');

  content.innerHTML = `
    <div class="stats-grid">${topCards}</div>
    <div class="section-title" style="margin-bottom:12px">By Status</div>
    <div class="stats-grid">${statusCards}</div>
    ${projectRows ? `<div class="section-title" style="margin:24px 0 12px">By Project</div>
      <div style="background:var(--surface);border-radius:10px;padding:8px 16px;box-shadow:0 1px 4px rgba(0,0,0,0.07)">${projectRows}</div>` : ''}`;
}

// ── Search ────────────────────────────────────────────────────────────────

function onSearch() {
  searchQuery = document.getElementById('search-input')?.value.toLowerCase().trim() || '';
  renderTasks();
}

// ── Export CSV ────────────────────────────────────────────────────────────

function exportCSV() {
  const scope = activeSheet
    ? (allTasks[activeSheet] ? { [activeSheet]: allTasks[activeSheet] } : {})
    : allTasks;
  const rows = [['Project','Task','Deadline','Hours','Status','Assignee','Completed Date']];
  Object.entries(scope).forEach(([sheet, tasks]) =>
    tasks.forEach(t => rows.push([sheet, t.task, t.deadline, t.hours, t.status, t.assignee, t.completed_date]))
  );
  const csv = rows.map(r => r.map(v => `"${String(v||'').replace(/"/g,'""')}"`).join(',')).join('\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
  a.download = (activeSheet || 'all-tasks') + '.csv';
  a.click();
}

// ── Bulk status ───────────────────────────────────────────────────────────

function onTaskSelect() {
  selectedTasks.clear();
  document.querySelectorAll('.task-check:checked').forEach(cb =>
    selectedTasks.add(cb.dataset.sheet + '::' + cb.dataset.row)
  );
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  if (!bar) return;
  const n = selectedTasks.size;
  bar.classList.toggle('visible', n > 0);
  const el = document.getElementById('bulk-count');
  if (el) el.textContent = `${n} task${n!==1?'s':''} selected`;
}

async function bulkMark(status) {
  if (!selectedTasks.size) return;
  await Promise.all([...selectedTasks].map(k => {
    const [sheet, row] = k.split('::');
    return fetch(`/api/tasks/${encodeURIComponent(sheet)}/${row}`, {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ status })
    });
  }));
  clearBulkSelect();
  await Promise.all([loadSheets(), loadTasks(true)]);
}

async function bulkDelete() {
  if (!selectedTasks.size) return;
  if (!confirm(`Permanently delete ${selectedTasks.size} task${selectedTasks.size !== 1 ? 's' : ''}?`)) return;
  await Promise.all([...selectedTasks].map(k => {
    const [sheet, row] = k.split('::');
    return fetch(`/api/tasks/${encodeURIComponent(sheet)}/${row}`, { method: 'DELETE' });
  }));
  clearBulkSelect();
  await Promise.all([loadSheets(), loadTasks(true)]);
}

function clearBulkSelect() {
  selectedTasks.clear();
  document.querySelectorAll('.task-check').forEach(cb => { cb.checked = false; });
  updateBulkBar();
}

// ── Date helpers ──────────────────────────────────────────────────────────

function toDateInputValue(str) {
  const d = parseDeadline(str);
  if (!d) return '';
  return d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0');
}

function fromDateInput(val) {
  if (!val) return '';
  const [y,m,d] = val.split('-').map(Number);
  return new Date(y, m-1, d).toLocaleDateString('en-GB', {day:'numeric', month:'long', year:'numeric'});
}

// ── Procrastination / Snake ───────────────────────────────────────────────

const CELL = 20, SCOLS = 21, SROWS = 21;

let _snakeLoop   = null;
let _snakeDir    = { x: 1, y: 0 };
let _snakeNext   = { x: 1, y: 0 };
let _snakeBody   = [];
let _snakeFood   = { x: 5, y: 5 };
let _snakeScore  = 0;
let _snakeAlive  = false;
let _snakeCanvas = null;
let _snakeCtx    = null;
let _procrastTimer    = null;
let _procrastSecsLeft = 0;
let _procrastRunning  = false;

const FUNNY_MSGS = [
  { emoji: '😬', text: "TIME'S UP.",                     sub: "Your future self is filing a formal complaint." },
  { emoji: '📉', text: "Productivity: critically low.",   sub: "The snake consumed your afternoon. No refunds." },
  { emoji: '💀', text: "Error 404: Work ethic not found.",sub: "Have you tried turning yourself off and on again?" },
  { emoji: '⏰', text: "Your deadline did not move.",     sub: "The earth did, however. Several miles." },
  { emoji: '👁️',  text: "Your advisor just had a feeling.",sub: "It was not a good one." },
  { emoji: '🐍', text: "The snake is satisfied.",         sub: "The research paper, regrettably, is not." },
  { emoji: '🏆', text: "Achievement unlocked:",           sub: "Expert-level avoidance. Certificate incoming." },
  { emoji: '🤔', text: "You could've written a paragraph.",sub: "You chose the snake. Honestly? Respect." },
  { emoji: '🌍', text: "Fun fact:",                       sub: "Procrastinating does not, technically, extend deadlines." },
  { emoji: '🎮', text: "Game over. Reality resumes.",     sub: "In 3... 2... 1... now. Go. Seriously." },
];

const DEEP_QUOTES = [
  { text: "You do not rise to the level of your goals. You fall to the level of your systems.", author: "James Clear" },
  { text: "Every action you take is a vote for the type of person you wish to become.", author: "James Clear" },
  { text: "Your outcomes are a lagging measure of your habits. Your net worth is a lagging measure of your financial habits. Your knowledge is a lagging measure of your learning habits.", author: "James Clear" },
  { text: "The first principle is that you must not fool yourself — and you are the easiest person to fool.", author: "Richard Feynman" },
  { text: "I would rather have questions that can't be answered than answers that can't be questioned.", author: "Richard Feynman" },
  { text: "Play long-term games with long-term people. All returns in life — wealth, relationships, knowledge — come from compound interest.", author: "Naval Ravikant" },
  { text: "Specific knowledge is found by pursuing your genuine curiosity and passion rather than whatever is hot right now.", author: "Naval Ravikant" },
  { text: "If you do not work on important problems, it's not likely that you'll do important work. It's perfectly obvious.", author: "Richard Hamming" },
  { text: "Waste no more time arguing what a good person should be. Be one.", author: "Marcus Aurelius" },
  { text: "The impediment to action advances action. What stands in the way becomes the way.", author: "Marcus Aurelius" },
  { text: "You have power over your mind, not outside events. Realize this, and you will find strength.", author: "Marcus Aurelius" },
  { text: "It's not what happens to you, but how you react to it that matters.", author: "Epictetus" },
  { text: "The ability to perform deep work is becoming increasingly rare at exactly the same time it is becoming increasingly valuable.", author: "Cal Newport" },
  { text: "Who you are, what you think, feel, and do, what you love — is the sum of what you focus on.", author: "Cal Newport" },
  { text: "Develop into a lifelong self-learner through voracious reading; cultivate curiosity and strive to become a little wiser every day.", author: "Charlie Munger" },
  { text: "The really important kind of freedom involves attention, awareness, discipline, and being able truly to care about other people and sacrifice for them, over and over, in myriad petty little unsexy ways, every day.", author: "David Foster Wallace" },
  { text: "A ship in harbour is safe, but that is not what ships are for.", author: "John A. Shedd" },
  { text: "We are what we repeatedly do. Excellence, then, is not an act, but a habit.", author: "Aristotle" },
  { text: "Do not pray for an easy life; pray for the strength to endure a difficult one.", author: "Bruce Lee" },
  { text: "Reading is the foundation for thinking. It's how you build the vocabulary and mental models to understand the world.", author: "Naval Ravikant" },
];

function cleanupProcrastinate() {
  if (_snakeLoop)     { clearInterval(_snakeLoop);    _snakeLoop    = null; }
  if (_procrastTimer) { clearInterval(_procrastTimer); _procrastTimer = null; }
  _snakeAlive      = false;
  _procrastRunning = false;
  document.removeEventListener('keydown', _snakeKeyHandler);
}

function _snakeKeyHandler(e) {
  const map = {
    ArrowUp:    {x:0,y:-1}, ArrowDown:  {x:0,y:1},
    ArrowLeft:  {x:-1,y:0}, ArrowRight: {x:1,y:0},
    w:{x:0,y:-1}, s:{x:0,y:1}, a:{x:-1,y:0}, d:{x:1,y:0},
  };
  const nd = map[e.key];
  if (nd) {
    if (nd.x !== -_snakeDir.x || nd.y !== -_snakeDir.y) _snakeNext = nd;
    if (e.key.startsWith('Arrow')) e.preventDefault();
  }
  if (e.key === ' ') {
    e.preventDefault();
    if (!_snakeAlive && _procrastRunning) _startSnake();
  }
}

function renderProcrastinate() {
  cleanupProcrastinate();
  const content = document.getElementById('content');
  const W = SCOLS * CELL, H = SROWS * CELL;
  content.innerHTML = `
    <div class="procrastinate-layout">
      <div class="snake-section">
        <div class="snake-header">
          <div class="snake-meta">
            <span class="snake-score-label">Score: <b id="snake-score">0</b></span>
          </div>
          <span id="procrastinate-timer" class="procrastinate-timer">5:00</span>
          <div class="snake-controls">
            <select id="timer-duration" onchange="_updateTimerDisplay(parseInt(this.value))">
              <option value="60">1 min</option>
              <option value="120">2 min</option>
              <option value="180">3 min</option>
              <option value="240">4 min</option>
              <option value="300" selected>5 min</option>
            </select>
            <button class="btn-save" id="snake-start-btn" onclick="startProcrastinate()">▶ Start</button>
            <button class="btn-cancel" onclick="resetProcrastinate()">↺ Reset</button>
          </div>
        </div>
        <canvas id="snake-canvas" width="${W}" height="${H}"></canvas>
        <div class="snake-hint">Arrow keys or WASD &nbsp;·&nbsp; Space to restart after game over</div>
      </div>
      <div class="quotes-section">
        <div class="section-title" style="margin-bottom:12px">Deep Thoughts</div>
        <div class="quotes-list" id="quotes-list"></div>
      </div>
    </div>`;
  _snakeCanvas = document.getElementById('snake-canvas');
  _snakeCtx    = _snakeCanvas.getContext('2d');
  document.addEventListener('keydown', _snakeKeyHandler);
  _drawSnakeIdle();
  _renderQuotes();
}

function _renderQuotes() {
  const el = document.getElementById('quotes-list');
  if (!el) return;
  const shuffled = [...DEEP_QUOTES].sort(() => Math.random() - 0.5);
  el.innerHTML = shuffled.map(q =>
    `<div class="quote-card">
      <div class="quote-text">&ldquo;${escHtml(q.text)}&rdquo;</div>
      <div class="quote-author">— ${escHtml(q.author)}</div>
    </div>`
  ).join('');
}

function _drawSnakeIdle() {
  const ctx = _snakeCtx, W = SCOLS*CELL, H = SROWS*CELL;
  ctx.fillStyle = '#0f0f1a'; ctx.fillRect(0,0,W,H);
  _drawGrid(ctx, W, H);
  ctx.textAlign = 'center';
  ctx.fillStyle = 'rgba(255,255,255,0.85)';
  ctx.font = 'bold 22px sans-serif';
  ctx.fillText('🐍  Snake', W/2, H/2 - 16);
  ctx.font = '13px sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.4)';
  ctx.fillText('Press ▶ Start to begin', W/2, H/2 + 14);
}

function _drawGrid(ctx, W, H) {
  ctx.strokeStyle = 'rgba(255,255,255,0.04)'; ctx.lineWidth = 0.5;
  for (let x = 0; x <= SCOLS; x++) { ctx.beginPath(); ctx.moveTo(x*CELL,0); ctx.lineTo(x*CELL,H); ctx.stroke(); }
  for (let y = 0; y <= SROWS; y++) { ctx.beginPath(); ctx.moveTo(0,y*CELL); ctx.lineTo(W,y*CELL); ctx.stroke(); }
}

function _randomFood() {
  let p;
  do { p = { x: Math.floor(Math.random()*SCOLS), y: Math.floor(Math.random()*SROWS) }; }
  while (_snakeBody.some(s => s.x === p.x && s.y === p.y));
  return p;
}

function _startSnake() {
  if (_snakeLoop) clearInterval(_snakeLoop);
  _snakeBody  = [{x:10,y:10},{x:9,y:10},{x:8,y:10}];
  _snakeDir   = {x:1,y:0}; _snakeNext = {x:1,y:0};
  _snakeFood  = _randomFood();
  _snakeScore = 0; _snakeAlive = true;
  const el = document.getElementById('snake-score'); if (el) el.textContent = '0';
  _snakeLoop  = setInterval(_snakeTick, 130);
}

function _snakeTick() {
  _snakeDir = _snakeNext;
  const head = { x: _snakeBody[0].x + _snakeDir.x, y: _snakeBody[0].y + _snakeDir.y };
  if (head.x < 0 || head.x >= SCOLS || head.y < 0 || head.y >= SROWS) return _snakeDie('You hit the wall.');
  if (_snakeBody.some(s => s.x === head.x && s.y === head.y))          return _snakeDie('You ate yourself.');
  _snakeBody.unshift(head);
  if (head.x === _snakeFood.x && head.y === _snakeFood.y) {
    _snakeScore++;
    const el = document.getElementById('snake-score'); if (el) el.textContent = _snakeScore;
    _snakeFood = _randomFood();
  } else { _snakeBody.pop(); }
  _drawSnakeLive();
}

function _drawSnakeLive() {
  const ctx = _snakeCtx, W = SCOLS*CELL, H = SROWS*CELL, len = _snakeBody.length;
  ctx.fillStyle = '#0f0f1a'; ctx.fillRect(0,0,W,H);
  _drawGrid(ctx, W, H);
  // food
  const fx = _snakeFood.x*CELL+CELL/2, fy = _snakeFood.y*CELL+CELL/2;
  ctx.fillStyle = '#FF6B6B';
  ctx.beginPath(); ctx.arc(fx, fy, CELL/2-2, 0, Math.PI*2); ctx.fill();
  ctx.fillStyle = 'rgba(255,255,255,0.35)';
  ctx.beginPath(); ctx.arc(fx-3, fy-3, 3, 0, Math.PI*2); ctx.fill();
  // snake
  _snakeBody.forEach((seg, i) => {
    const t = i / len;
    ctx.fillStyle = i === 0 ? '#68D391' : `hsl(145,${Math.round(55-t*20)}%,${Math.round(50-t*18)}%)`;
    const x = seg.x*CELL+1, y = seg.y*CELL+1, s = CELL-2;
    ctx.beginPath(); ctx.roundRect(x, y, s, s, 3); ctx.fill();
  });
}

function _snakeDie(reason) {
  clearInterval(_snakeLoop); _snakeLoop = null; _snakeAlive = false;
  _drawSnakeLive();
  const ctx = _snakeCtx, W = SCOLS*CELL, H = SROWS*CELL;
  ctx.fillStyle = 'rgba(0,0,0,0.6)'; ctx.fillRect(0,0,W,H);
  ctx.textAlign = 'center';
  ctx.fillStyle = '#FC8181'; ctx.font = 'bold 18px sans-serif'; ctx.fillText('GAME OVER', W/2, H/2-22);
  ctx.fillStyle = 'rgba(255,255,255,0.6)'; ctx.font = '12px sans-serif'; ctx.fillText(reason, W/2, H/2+2);
  ctx.fillStyle = 'rgba(255,255,255,0.35)'; ctx.fillText('Space to restart', W/2, H/2+28);
}

function startProcrastinate() {
  const dur = parseInt(document.getElementById('timer-duration')?.value || '300');
  _procrastSecsLeft = dur; _procrastRunning = true;
  _updateTimerDisplay(dur);
  if (_procrastTimer) clearInterval(_procrastTimer);
  _procrastTimer = setInterval(() => {
    _procrastSecsLeft--;
    _updateTimerDisplay(_procrastSecsLeft);
    if (_procrastSecsLeft <= 0) { clearInterval(_procrastTimer); _procrastTimer = null; _procrastRunning = false; _timeUp(); }
  }, 1000);
  const btn = document.getElementById('snake-start-btn'); if (btn) btn.disabled = true;
  _startSnake();
}

function resetProcrastinate() {
  cleanupProcrastinate();
  const dur = parseInt(document.getElementById('timer-duration')?.value || '300');
  _updateTimerDisplay(dur);
  const btn = document.getElementById('snake-start-btn'); if (btn) btn.disabled = false;
  _drawSnakeIdle();
}

function _updateTimerDisplay(secs) {
  const el = document.getElementById('procrastinate-timer'); if (!el) return;
  const m = Math.floor(secs/60), s = secs%60;
  el.textContent = `${m}:${s.toString().padStart(2,'0')}`;
  el.className = 'procrastinate-timer' + (secs<=30?' danger': secs<=60?' warning':'');
}

function _timeUp() {
  if (_snakeLoop) { clearInterval(_snakeLoop); _snakeLoop = null; } _snakeAlive = false;
  if (_snakeCtx) {
    const W=SCOLS*CELL, H=SROWS*CELL;
    _snakeCtx.fillStyle='rgba(0,0,0,0.72)'; _snakeCtx.fillRect(0,0,W,H);
    _snakeCtx.fillStyle='#F6C90E'; _snakeCtx.font='bold 20px sans-serif'; _snakeCtx.textAlign='center';
    _snakeCtx.fillText("TIME'S UP", W/2, H/2);
  }
  const msg = FUNNY_MSGS[Math.floor(Math.random()*FUNNY_MSGS.length)];
  document.getElementById('po-emoji').textContent = msg.emoji;
  document.getElementById('po-text').textContent  = msg.text;
  document.getElementById('po-sub').textContent   = msg.sub;
  document.getElementById('procrastinate-overlay').classList.add('open');
  const btn = document.getElementById('snake-start-btn'); if (btn) btn.disabled = false;
}

function closeProcrastinateOverlay() {
  document.getElementById('procrastinate-overlay').classList.remove('open');
}

// ── Helpers ───────────────────────────────────────────────────────────────

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// Use jsStr() — not escHtml() — when embedding a value inside a JS string literal
// in an inline onclick attribute. HTML entities like &#39; get decoded by the HTML
// parser *before* JS sees them, which would break out of the string literal.
// Backslash-escaping is invisible to the HTML parser and safe for JS.
function jsStr(s) {
  return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")
                  .replace(/\n/g,'\\n').replace(/\r/g,'\\r');
}

// ── Sidebar resize ────────────────────────────────────────────────────────
(function() {
  const sidebar = document.getElementById('sidebar');
  const handle  = document.getElementById('sidebar-resize');
  const saved = localStorage.getItem('sidebarWidth');
  const setSidebarW = w => {
    sidebar.style.width = w + 'px';
    document.documentElement.style.setProperty('--sidebar-w', w + 'px');
  };
  if (saved) setSidebarW(parseInt(saved));
  let dragging = false, startX = 0, startW = 0;
  handle.addEventListener('mousedown', e => {
    dragging = true; startX = e.clientX; startW = sidebar.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cssText += 'cursor:col-resize;user-select:none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    setSidebarW(Math.max(160, Math.min(400, startW + e.clientX - startX)));
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false; handle.classList.remove('dragging');
    document.body.style.cursor = ''; document.body.style.userSelect = '';
    localStorage.setItem('sidebarWidth', sidebar.offsetWidth);
  });
})();

init();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:8080")).start()
    app.run(host='127.0.0.1', port=8080, debug=False)
