#!/usr/bin/env python3
"""Browser-based task manager for research Google Sheet."""

import time
import datetime
import threading
import webbrowser
import json
import os
import re
import sys
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

# REMOTE_ENABLED: a Google Sheet is configured AND gspread is importable, so we
# can mirror to Sheets. When True the app runs in DUAL mode (local mirror is the
# read/source of truth; every edit is also written to Sheets live, or journalled
# to a pending queue and replayed when the connection returns). When False the
# app is pure-local (LOCAL_MODE) exactly as before.
REMOTE_ENABLED  = bool(SHEET_ID) and _GSPREAD_AVAILABLE
LOCAL_MODE      = not REMOTE_ENABLED

SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
STATUSES = ["Not Started", "In Progress", "Pending", "Completed"]
ACTIVE_STATUSES = {"Not Started", "In Progress", "Pending"}
DATA_CACHE_TTL = float('inf')  # never auto-expire; use /api/sync to force refresh
SYNC_INTERVAL  = 20   # seconds between background attempts to drain the pending queue

NOTES_SHEET    = "_notes"
COLLABS_SHEET  = "_collabs"
LIT_SHEET      = "_literature"
PLAN_SHEET     = "_plan"
META_SHEETS    = {NOTES_SHEET, COLLABS_SHEET, LIT_SHEET, PLAN_SHEET}
NOTE_COLORS    = ["#FFF9C4", "#C8E6C9", "#BBDEFB", "#F8BBD0", "#E1BEE7", "#FFE0B2"]
IMPORTANCES    = ["High", "Medium", "Low"]
PURPOSES       = ["Design", "Writing", "Analysis", "Planning", "Other"]
NOTE_HEADERS   = ["Project", "Note", "Importance", "Purpose", "Color", "Created", "Modified"]
COLLAB_HEADERS = ["Project", "Name", "Role"]
LIT_HEADERS    = ["Project", "Title", "Link", "Authors", "Year", "Notes", "Created", "Modified"]
PLAN_HEADERS   = ["Project", "TaskRow", "Day", "Order", "Hours", "Created", "Modified"]

app = Flask(__name__)
_sheet_cache  = None
_fetch_lock   = threading.Lock()
_sync_lock    = threading.RLock()   # guards _pending_ops + remote writes
_ws_cache     = {}    # {title: _LocalWS/_DualWS} — write handles returned to routes
_remote_ws_cache = {} # {title: gspread Worksheet} — real remote handles (dual mode)
_data_cache   = {}    # {title: [rows]} — project sheets only
_notes_cache  = []    # rows from _notes sheet
_collabs_cache= []    # rows from _collabs sheet
_lit_cache    = []    # rows from _literature sheet
_plan_cache   = []    # rows from _plan sheet
_data_cache_ts= 0.0

# Offline journal + sync state (dual mode only)
_pending_ops  = []      # ordered list of {kind, title, ...} not yet applied to Sheets
_online       = True    # last known reachability of Google Sheets
_last_synced  = None    # ISO timestamp of the last successful full drain
_sync_thread_started = False


# ── Local storage shim ────────────────────────────────────────────────────
# The in-memory caches are the single source of truth for reads. _LocalWS makes
# a mutation route write to those caches + the on-disk mirror using the same
# ws.update()/append_row()/delete_rows() calls it would make against gspread.

def _meta_cache_for(title):
    """Return the module-level cache list for a meta sheet, or None for a project sheet."""
    if title == NOTES_SHEET:   return _notes_cache
    if title == COLLABS_SHEET: return _collabs_cache
    if title == LIT_SHEET:     return _lit_cache
    if title == PLAN_SHEET:    return _plan_cache
    return None


class _LocalWS:
    """Mimics a gspread Worksheet so mutation routes work unchanged with no network."""
    def __init__(self, title, is_notes=False, is_collabs=False, is_lit=False, is_plan=False):
        self.title     = title
        self._notes    = is_notes
        self._collabs  = is_collabs
        self._lit      = is_lit
        self._plan     = is_plan

    def _row_num(self, range_name):
        m = re.search(r'\d+', range_name)
        return int(m.group()) if m else 1

    def _meta_list(self):
        return _meta_cache_for(self.title)

    def update(self, range_name, values):
        row = self._row_num(range_name)
        target = self._meta_list()
        if target is not None:
            while len(target) < row: target.append([])
            target[row - 1] = list(values[0])
        else:
            patch_cache(self.title, row, values[0])
        _local_save()

    def append_row(self, values):
        target = self._meta_list()
        if target is not None:
            target.append(list(values))
        else:
            _data_cache.setdefault(self.title, []).append(list(values))
        _local_save()

    def delete_rows(self, row):
        target = self._meta_list()
        if target is None:
            target = _data_cache.get(self.title, [])
        if 0 < row <= len(target):
            target.pop(row - 1)
        _local_save()


class _DualWS(_LocalWS):
    """Dual mode: apply the mutation to the local mirror (via _LocalWS) AND mirror
    it to Google Sheets — immediately when online, or onto the pending journal when
    offline. Reads never come from here; they come from the caches _LocalWS fills."""
    def update(self, range_name, values):
        super().update(range_name, values)
        _enqueue_or_apply({"kind": "update", "title": self.title,
                           "range": range_name, "values": values})

    def append_row(self, values):
        super().append_row(values)
        _enqueue_or_apply({"kind": "append", "title": self.title, "values": list(values)})

    def delete_rows(self, row):
        super().delete_rows(row)
        _enqueue_or_apply({"kind": "delete", "title": self.title, "row": row})


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


MIRROR_SCHEMA = 2   # bump when the mirror layout changes; v1 predates dual/offline mode
_loaded_schema = 0  # schema of the mirror last read by _local_load()


def _local_load():
    """Populate caches (and the pending journal) from the on-disk mirror.
    Returns True if the mirror existed and held any data."""
    global _data_cache, _notes_cache, _collabs_cache, _lit_cache, _plan_cache
    global _pending_ops, _last_synced, _loaded_schema
    _loaded_schema = 0
    if not os.path.exists(LOCAL_DATA_FILE):
        return False
    with open(LOCAL_DATA_FILE) as f:
        d = json.load(f)
    _data_cache    = d.get("projects", {})
    _notes_cache   = d.get("notes",    [])
    _collabs_cache = d.get("collabs",  [])
    _lit_cache     = d.get("literature", [])
    _plan_cache    = d.get("plan",     [])
    _pending_ops   = d.get("pending",  [])
    _last_synced   = d.get("last_synced")
    _loaded_schema = d.get("schema", 1)   # files written before this feature have no schema key
    return bool(_data_cache or _notes_cache or _collabs_cache or _lit_cache or _plan_cache)


def _local_save():
    """Atomically persist all caches + the pending journal to the mirror file.
    Uses a per-thread temp name so a concurrent writer (e.g. the sync daemon) can't
    clobber our temp file; os.replace makes the final swap atomic, last write wins."""
    tmp = f"{LOCAL_DATA_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"schema": MIRROR_SCHEMA,
                   "projects": _data_cache, "notes": _notes_cache,
                   "collabs": _collabs_cache, "literature": _lit_cache,
                   "plan": _plan_cache, "pending": _pending_ops,
                   "last_synced": _last_synced}, f, indent=2)
    os.replace(tmp, LOCAL_DATA_FILE)   # atomic: a crash mid-write can't corrupt the mirror


# ── Google Sheets helpers ─────────────────────────────────────────────────

def get_sheet():
    global _sheet_cache
    if _sheet_cache is None:
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        gc = gspread.authorize(creds)
        _sheet_cache = gc.open_by_key(SHEET_ID)
    return _sheet_cache


def _headers_for(title):
    return {NOTES_SHEET: NOTE_HEADERS, COLLABS_SHEET: COLLAB_HEADERS,
            LIT_SHEET: LIT_HEADERS, PLAN_SHEET: PLAN_HEADERS}.get(title, TASK_HEADERS)


def _remote_ws(title):
    """Real gspread worksheet for `title`, creating the tab (with headers) if missing."""
    if title in _remote_ws_cache:
        return _remote_ws_cache[title]
    sh = get_sheet()
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        headers = _headers_for(title)
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(10, len(headers)))
        ws.update(range_name="A1", values=[headers])
    _remote_ws_cache[title] = ws
    return ws


def _apply_remote_op(op):
    """Perform one journalled op against Google Sheets. Raises on failure."""
    kind = op["kind"]
    if kind == "update":
        _remote_ws(op["title"]).update(range_name=op["range"], values=op["values"])
    elif kind == "append":
        _remote_ws(op["title"]).append_row(op["values"])
    elif kind == "delete":
        _remote_ws(op["title"]).delete_rows(op["row"])
    elif kind == "create_sheet":
        _remote_ws(op["title"])   # creation (with headers) is the whole operation
    elif kind == "delete_sheet":
        sh = get_sheet()
        try:
            sh.del_worksheet(sh.worksheet(op["title"]))
        except gspread.exceptions.WorksheetNotFound:
            pass
        _remote_ws_cache.pop(op["title"], None)


def _mark_online(v):
    global _online
    _online = v


def _enqueue_or_apply(op):
    """Mirror one op to Sheets now if we're caught up and online; else journal it."""
    if not REMOTE_ENABLED:
        return
    with _sync_lock:
        if _pending_ops:            # already behind — preserve strict ordering
            _pending_ops.append(op)
            _local_save()
            return
        try:
            _apply_remote_op(op)
            _mark_online(True)
        except Exception as e:      # noqa: BLE001 — any failure means "not now", never lose the edit
            _pending_ops.append(op)
            _local_save()
            _mark_online(False)
            print(f"[sync] queued {op.get('kind')} on {op.get('title')} ({e.__class__.__name__})", file=sys.stderr)


def _drain_pending():
    """Replay the journal to Sheets in order. Stops at the first failure, keeping the rest queued."""
    global _last_synced
    if not REMOTE_ENABLED:
        return
    with _sync_lock:
        if not _pending_ops:
            return
        while _pending_ops:
            op = _pending_ops[0]
            try:
                _apply_remote_op(op)
            except Exception as e:  # noqa: BLE001 — still offline / transient; try again later
                _mark_online(False)
                print(f"[sync] drain paused: {e.__class__.__name__}", file=sys.stderr)
                return
            _pending_ops.pop(0)
            _local_save()
        _mark_online(True)
        _last_synced = _now_iso()
        _local_save()


def _try_pull_from_remote():
    """One-time seed: pull every tab from Sheets into the caches + mirror.
    Used only when the local mirror is empty (first run / fresh machine)."""
    global _data_cache, _notes_cache, _collabs_cache, _lit_cache, _plan_cache
    for attempt in range(5):
        try:
            sh = get_sheet()
            worksheets = sh.worksheets()
            _remote_ws_cache.clear()
            _remote_ws_cache.update({ws.title: ws for ws in worksheets})
            raw = {ws.title: ws.get_all_values() for ws in worksheets}
            _data_cache    = {k: v for k, v in raw.items() if k not in META_SHEETS}
            _notes_cache   = raw.get(NOTES_SHEET,  [])
            _collabs_cache = raw.get(COLLABS_SHEET, [])
            _lit_cache     = raw.get(LIT_SHEET, [])
            _plan_cache    = raw.get(PLAN_SHEET, [])
            _mark_online(True)
            _local_save()
            return True
        except gspread.exceptions.APIError as e:
            if getattr(e, "response", None) is not None and e.response.status_code == 429 and attempt < 4:
                time.sleep(2 ** attempt)
            else:
                _mark_online(False)
                return False
        except Exception:           # noqa: BLE001 — offline first run
            _mark_online(False)
            return False
    return False


def _sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL)
        try:
            if _pending_ops:
                _drain_pending()
        except Exception as e:      # noqa: BLE001 — never let the daemon die
            print(f"[sync] loop error: {e.__class__.__name__}", file=sys.stderr)


def _start_sync_thread_once():
    global _sync_thread_started
    if _sync_thread_started or not REMOTE_ENABLED:
        return
    _sync_thread_started = True
    threading.Thread(target=_sync_loop, daemon=True).start()


def _ensure_loaded():
    """Load caches from the mirror on first use; seed from Sheets if the mirror is empty."""
    global _data_cache_ts
    if _data_cache_ts > 0:
        return
    with _fetch_lock:
        if _data_cache_ts > 0:
            return
        had = _local_load()
        if REMOTE_ENABLED:
            # Trust the mirror only if it was written by this dual/offline version.
            # A pre-dual mirror (schema < 2) is ignored so the first dual launch
            # re-seeds cleanly from the real Sheet instead of showing stale data.
            if not had or _loaded_schema < MIRROR_SCHEMA:
                _reset_caches()           # drop any stale pre-dual rows before seeding
                _try_pull_from_remote()   # offline first run just leaves it empty until a later sync
            _start_sync_thread_once()
            _drain_pending()              # catch up anything journalled before a restart
        _data_cache_ts = time.time()


def _fetch_all():
    """Back-compat shim: ensure caches are loaded (no forced network re-read)."""
    _ensure_loaded()


def get_all_sheet_data():
    _ensure_loaded()
    return _data_cache


def get_notes_data():
    _ensure_loaded()
    return _notes_cache


def get_collabs_data():
    _ensure_loaded()
    return _collabs_cache


def get_literature_data():
    _ensure_loaded()
    return _lit_cache


def get_plan_data():
    _ensure_loaded()
    return _plan_cache


def _make_ws(title, **flags):
    return _DualWS(title, **flags) if REMOTE_ENABLED else _LocalWS(title, **flags)


def get_worksheet(title):
    _ensure_loaded()
    if title not in _data_cache:
        raise KeyError(title)
    return _make_ws(title)


def ensure_meta_ws(title, headers):
    """Get a write handle for a meta sheet, seeding its header row locally if new."""
    _ensure_loaded()
    flags = {"is_notes": title == NOTES_SHEET, "is_collabs": title == COLLABS_SHEET,
             "is_lit": title == LIT_SHEET, "is_plan": title == PLAN_SHEET}
    cache = _meta_cache_for(title)
    if cache is not None and not cache:
        cache.append(list(headers))   # seed header row in place (row 1)
        _local_save()
        # Remote tab (with the same header row) is created lazily by _remote_ws on
        # the first journalled op, so no header op needs to be enqueued here.
    return _make_ws(title, **flags)


def sync_status():
    return {
        "remote_enabled": REMOTE_ENABLED,
        "local_mode": LOCAL_MODE,
        "online": _online,
        "pending": len(_pending_ops),
        "last_synced": _last_synced,
    }


def _reset_caches():
    """Empty the data caches (not the pending journal or timestamp)."""
    global _data_cache, _notes_cache, _collabs_cache, _lit_cache, _plan_cache
    _data_cache    = {}
    _notes_cache   = []
    _collabs_cache = []
    _lit_cache     = []
    _plan_cache    = []


def invalidate_cache():
    global _data_cache_ts
    _reset_caches()
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


# ── Meta-cache helpers (notes/collabs/literature) ───────────────────────────
# Let a single note/collab/literature edit patch its own cache array in place
# instead of invalidate_cache() forcing a full re-fetch of every worksheet on
# the next request.
#
# patch_meta_cache is idempotent (same row, same values) so it's safe to call
# unconditionally after ws.update() — same as patch_cache() already being
# called unconditionally after both real and _LocalWS task updates, even
# though _LocalWS.update() also patches the cache itself.
#
# append/delete are NOT idempotent — calling them twice would duplicate or
# over-remove a row — and _LocalWS.append_row()/delete_rows() already apply
# the same mutation to the cache as a side effect in local mode. So these two
# only need to run for the real-Sheets `ws` (a plain gspread Worksheet has no
# such side effect).

def patch_meta_cache(cache_list, row, values):
    idx = row - 1
    while len(cache_list) <= idx:
        cache_list.append([])
    cache_list[idx] = list(values)


def append_meta_cache(cache_list, ws, values):
    if not isinstance(ws, _LocalWS):
        cache_list.append(list(values))


def delete_meta_cache_row(cache_list, ws, row):
    if not isinstance(ws, _LocalWS) and 0 < row <= len(cache_list):
        cache_list.pop(row - 1)


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


def parse_literature(rows):
    lit = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 2 or not row[1].strip():
            continue
        lit.append({
            "row":      i,
            "project":  row[0].strip() if len(row) > 0 else "",
            "title":    row[1].strip(),
            "link":     row[2].strip() if len(row) > 2 else "",
            "authors":  row[3].strip() if len(row) > 3 else "",
            "year":     row[4].strip() if len(row) > 4 else "",
            "notes":    row[5].strip() if len(row) > 5 else "",
            "created":  row[6].strip() if len(row) > 6 else "",
            "modified": row[7].strip() if len(row) > 7 else "",
        })
    return lit


def parse_plan(rows):
    plan = []
    for i, row in enumerate(rows[1:], start=2):
        # Project, TaskRow, Day, Order, Hours, Created, Modified
        if len(row) < 3 or not row[0].strip() or not row[2].strip():
            continue
        def _int(v, d=0):
            try: return int(float(v))
            except (TypeError, ValueError): return d
        plan.append({
            "row":      i,
            "project":  row[0].strip(),
            "task_row": _int(row[1].strip() if len(row) > 1 else "", 0),
            "day":      row[2].strip(),          # ISO date "YYYY-MM-DD"
            "order":    _int(row[3].strip() if len(row) > 3 else "", 0),
            "hours":    row[4].strip() if len(row) > 4 else "",
            "created":  row[5].strip() if len(row) > 5 else "",
            "modified": row[6].strip() if len(row) > 6 else "",
        })
    return plan


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
    new_row = [
        data.get("project", ""), data.get("note", ""),
        data.get("importance", "Medium"), data.get("purpose", "Other"),
        data.get("color", NOTE_COLORS[0]), today, today,
    ]
    ws.append_row(new_row)
    append_meta_cache(_notes_cache, ws, new_row)
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
    patch_meta_cache(_notes_cache, row, updated)
    return jsonify({"ok": True})


@app.route("/api/notes/<int:row>", methods=["DELETE"])
def api_delete_note(row):
    ws = ensure_meta_ws(NOTES_SHEET, NOTE_HEADERS)
    ws.delete_rows(row)
    delete_meta_cache_row(_notes_cache, ws, row)
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
            new_row = [project, name, role]
            ws.append_row(new_row)
            append_meta_cache(_collabs_cache, ws, new_row)
            existing.add(name.lower())
    return jsonify({"ok": True})


@app.route("/api/collaborators/<int:row>", methods=["DELETE"])
def api_delete_collaborator(row):
    ws = ensure_meta_ws(COLLABS_SHEET, COLLAB_HEADERS)
    ws.delete_rows(row)
    delete_meta_cache_row(_collabs_cache, ws, row)
    return jsonify({"ok": True})


# ── Literature API ─────────────────────────────────────────────────────────

@app.route("/api/literature")
def api_literature():
    project = request.args.get("project")
    lit = parse_literature(get_literature_data())
    if project:
        lit = [l for l in lit if l["project"] == project]
    return jsonify(lit)


@app.route("/api/literature", methods=["POST"])
def api_add_literature():
    data = request.get_json(silent=True) or {}
    if not data.get("title") or not data.get("project"):
        return jsonify({"error": "project and title required"}), 400
    ws = ensure_meta_ws(LIT_SHEET, LIT_HEADERS)
    today = _today()
    new_row = [
        data["project"], data["title"], data.get("link", ""),
        data.get("authors", ""), data.get("year", ""), data.get("notes", ""),
        today, today,
    ]
    ws.append_row(new_row)
    append_meta_cache(_lit_cache, ws, new_row)
    return jsonify({"ok": True})


@app.route("/api/literature/<int:row>", methods=["PUT"])
def api_update_literature(row):
    data = request.get_json(silent=True) or {}
    ws = ensure_meta_ws(LIT_SHEET, LIT_HEADERS)
    rows = get_literature_data()
    current = list(rows[row - 1]) if row - 1 < len(rows) else []
    while len(current) < 8:
        current.append("")
    updated = [
        data.get("project", current[0]),
        data.get("title",   current[1]),
        data.get("link",    current[2]),
        data.get("authors", current[3]),
        data.get("year",    current[4]),
        data.get("notes",   current[5]),
        current[6],   # created unchanged
        _today(),     # modified = today
    ]
    ws.update(range_name=f"A{row}:H{row}", values=[updated])
    patch_meta_cache(_lit_cache, row, updated)
    return jsonify({"ok": True})


@app.route("/api/literature/<int:row>", methods=["DELETE"])
def api_delete_literature(row):
    ws = ensure_meta_ws(LIT_SHEET, LIT_HEADERS)
    ws.delete_rows(row)
    delete_meta_cache_row(_lit_cache, ws, row)
    return jsonify({"ok": True})


# ── Plan API (weekly planner: which task on which day, priority order, est. hours) ──

@app.route("/api/plan")
def api_plan():
    return jsonify(parse_plan(get_plan_data()))


@app.route("/api/plan", methods=["POST"])
def api_add_plan():
    data = request.get_json(silent=True) or {}
    project = (data.get("project") or "").strip()
    day     = (data.get("day") or "").strip()
    if not project or not day:
        return jsonify({"error": "project and day required"}), 400
    ws = ensure_meta_ws(PLAN_SHEET, PLAN_HEADERS)
    today = _today()
    new_row = [
        project, str(data.get("task_row", "")), day,
        str(data.get("order", 0)), str(data.get("hours", "")),
        today, today,
    ]
    ws.append_row(new_row)
    append_meta_cache(_plan_cache, ws, new_row)
    return jsonify({"ok": True})


@app.route("/api/plan/<int:row>", methods=["PUT"])
def api_update_plan(row):
    data = request.get_json(silent=True) or {}
    ws = ensure_meta_ws(PLAN_SHEET, PLAN_HEADERS)
    rows = get_plan_data()
    current = list(rows[row - 1]) if row - 1 < len(rows) else []
    while len(current) < 7:
        current.append("")
    updated = [
        data.get("project",  current[0]),
        str(data.get("task_row", current[1])),
        data.get("day",      current[2]),
        str(data.get("order", current[3])),
        str(data.get("hours", current[4])),
        current[5],   # created unchanged
        _today(),     # modified
    ]
    ws.update(range_name=f"A{row}:G{row}", values=[updated])
    patch_meta_cache(_plan_cache, row, updated)
    return jsonify({"ok": True})


@app.route("/api/plan/<int:row>", methods=["DELETE"])
def api_delete_plan(row):
    ws = ensure_meta_ws(PLAN_SHEET, PLAN_HEADERS)
    ws.delete_rows(row)
    delete_meta_cache_row(_plan_cache, ws, row)
    return jsonify({"ok": True})


# ── Sync API ──────────────────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Push any pending offline changes to Sheets and report sync state.
    Never overwrites the local mirror from Sheets — the mirror is the source of truth.
    (If the mirror is somehow still empty, do the one-time seed pull.)"""
    _ensure_loaded()
    if REMOTE_ENABLED:
        if not (_data_cache or _notes_cache or _collabs_cache or _lit_cache or _plan_cache):
            _try_pull_from_remote()
        _drain_pending()
    return jsonify({"ok": True, **sync_status()})


@app.route("/api/status")
def api_status():
    _ensure_loaded()
    return jsonify(sync_status())


# ── Projects API ──────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["POST"])
def api_create_project():
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    _ensure_loaded()
    if name in _data_cache:
        return jsonify({"error": "Project already exists"}), 409
    if name in META_SHEETS:
        return jsonify({"error": "Reserved name"}), 400
    _data_cache[name] = [list(TASK_HEADERS)]   # header row prevents row-1 collision
    _local_save()
    if REMOTE_ENABLED:
        _enqueue_or_apply({"kind": "create_sheet", "title": name})
    return jsonify({"ok": True, "name": name})


@app.route("/api/projects/<name>", methods=["DELETE"])
def api_delete_project(name):
    _ensure_loaded()
    if name not in _data_cache:
        return jsonify({"error": "Project not found"}), 404
    if name in META_SHEETS:
        return jsonify({"error": "Cannot delete meta sheet"}), 400
    _data_cache.pop(name, None)
    _local_save()
    if REMOTE_ENABLED:
        _enqueue_or_apply({"kind": "delete_sheet", "title": name})
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
    padding: 9px 12px 9px 8px; cursor: pointer; border-radius: 6px; margin: 1px 8px;
    display: flex; align-items: center; gap: 4px;
    color: var(--sidebar-text); font-size: 13px; transition: background 0.15s;
  }
  .sheet-item:hover { background: var(--sidebar-hover); color: #fff; }
  .sheet-item.active { background: rgba(90,103,216,0.35); color: var(--sidebar-active); font-weight: 600; }
  .sheet-item .badge { font-size: 10px; background: rgba(255,255,255,0.15);
                        color: #fff; padding: 1px 6px; border-radius: 10px; margin-left: auto; }
  .sheet-item .drag-handle {
    font-size: 13px; opacity: 0; cursor: grab; padding: 0 3px; flex-shrink: 0;
    transition: opacity 0.15s; user-select: none; line-height: 1;
  }
  .sheet-item:hover .drag-handle { opacity: 0.45; }
  .sheet-item.dragging { opacity: 0.4; }
  .sheet-item.drag-over { outline: 1px dashed rgba(255,255,255,0.35); border-radius: 6px; }
  .sidebar-sep { height: 1px; background: rgba(255,255,255,0.06); margin: 8px 16px; }
  #show-all, #upcoming-btn, #notes-btn, #literature-btn, #collabs-btn, #stats-btn, #procrastinate-btn, #gardone-btn {
    padding: 9px 20px; cursor: pointer; font-size: 13px; color: var(--sidebar-text);
    margin: 1px 8px; border-radius: 6px; display: flex; align-items: center; gap: 8px;
  }
  #show-all:hover, #upcoming-btn:hover, #notes-btn:hover, #literature-btn:hover, #collabs-btn:hover, #stats-btn:hover, #procrastinate-btn:hover, #gardone-btn:hover {
    background: var(--sidebar-hover); color: #fff;
  }
  #show-all.active, #upcoming-btn.active, #notes-btn.active, #literature-btn.active, #collabs-btn.active, #stats-btn.active, #procrastinate-btn.active, #gardone-btn.active {
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
    padding: 10px 28px 0; display: flex; align-items: center;
    gap: 10px; flex-shrink: 0; flex-wrap: wrap;
  }
  #topbar h2 { font-size: 16px; font-weight: 700; flex: 1; min-width: 0;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
               padding: 4px 0; }
  #topbar-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; padding: 4px 0; }
  #filter-area {
    order: 10; width: 100%;
    display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
    padding: 8px 0 10px;
    border-top: 1px solid var(--border);
  }
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
  .note-title { font-size: 14px; font-weight: 700; color: var(--text);
                 margin-bottom: 5px; word-break: break-word; }
  .note-body  { font-size: 13px; color: var(--text); line-height: 1.55;
                margin-bottom: 10px; word-break: break-word; }
  .note-body ul, .note-body ol { padding-left: 22px; margin: 4px 0; }
  #n-editor ul, #n-editor ol  { padding-left: 22px; margin: 4px 0; }
  .note-body p  { margin: 0 0 4px; }
  /* legacy plain-text notes */
  .note-text { font-size: 13px; color: var(--text); line-height: 1.55;
                margin-bottom: 10px; white-space: pre-wrap; word-break: break-word; }
  /* editor inside modal */
  #note-modal .modal { width: 540px; }
  .note-editor-toolbar {
    display: flex; align-items: center; gap: 3px; flex-wrap: wrap;
    padding: 6px 8px; background: var(--bg);
    border: 1px solid var(--border); border-bottom: none; border-radius: 8px 8px 0 0;
  }
  .net-btn {
    padding: 3px 8px; border-radius: 4px; border: 1px solid var(--border);
    background: var(--surface); cursor: pointer; font-size: 12px; font-weight: 600;
    color: var(--text); line-height: 1.4; transition: background 0.1s; min-width: 26px;
  }
  .net-btn:hover { background: var(--border); }
  .net-sep { width: 1px; height: 16px; background: var(--border); margin: 0 3px; flex-shrink: 0; }
  .net-select {
    padding: 3px 5px; border-radius: 4px; border: 1px solid var(--border);
    background: var(--surface); font-size: 12px; color: var(--text); cursor: pointer;
  }
  #n-editor {
    min-height: 120px; max-height: 260px; overflow-y: auto;
    border: 1px solid var(--border); border-radius: 0 0 8px 8px;
    padding: 10px 12px; font-size: 14px; line-height: 1.6;
    color: var(--text); background: var(--surface); outline: none;
  }
  #n-editor:focus { border-color: var(--accent); }
  #n-editor:empty::before { content: attr(data-placeholder); color: var(--text-muted); pointer-events: none; }
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

  /* ── Literature ── */
  .lit-search-input {
    padding: 6px 12px; border: 1.5px solid var(--border); border-radius: 20px;
    font-size: 13px; font-family: inherit; outline: none; width: 190px;
    background: var(--bg); color: var(--text); transition: border-color 0.15s, width 0.2s;
  }
  .lit-search-input:focus { border-color: var(--accent); width: 240px; }
  .lit-cards { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; align-items: start; }
  @media (max-width: 760px) {
    .lit-cards { grid-template-columns: 1fr; }
  }
  .lit-project-card {
    background: var(--surface); border-radius: 12px; overflow: hidden;
    border: 1.5px solid var(--border); border-top: 4px solid;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    transition: box-shadow 0.18s ease, transform 0.18s ease;
    display: flex; flex-direction: column;
  }
  .lit-project-card:hover { box-shadow: 0 6px 20px rgba(0,0,0,0.13); transform: translateY(-2px); }
  .lit-project-card .task-table { box-shadow: none; border-radius: 0; }
  .lit-project-card .task-row td { padding: 8px 10px; }
  .lit-card-header { display: flex; align-items: center; gap: 9px; padding: 9px 12px; }
  .lit-card-flower { flex-shrink: 0; display: flex; }
  .lit-card-heading { flex: 1; min-width: 0; }
  .lit-card-title { font-size: 13px; font-weight: 700; word-break: break-word; }
  .lit-card-count { font-size: 10px; font-weight: 700; text-transform: uppercase;
                     letter-spacing: 0.5px; margin-top: 1px; }
  .lit-card-body { max-height: 260px; overflow-y: auto; overflow-x: hidden; border-top: 1px solid var(--border); }
  .lit-title { font-size: 13px; color: var(--text); line-height: 1.4; word-break: break-word; overflow-wrap: anywhere; }
  .lit-title a { color: var(--accent); text-decoration: none; font-weight: 600; }
  .lit-title a:hover { text-decoration: underline; }
  .lit-note-panel {
    margin-top: 6px; padding: 7px 9px; background: var(--bg); border-radius: 6px;
    font-size: 11.5px; color: var(--text); line-height: 1.5;
    white-space: pre-wrap; word-break: break-word;
  }

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

  /* Demo mode toggle */
  .demo-toggle-wrap {
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 10px; padding: 6px 4px 0;
    border-top: 1px solid rgba(255,255,255,0.06);
  }
  .demo-toggle-label { font-size: 11px; color: rgba(255,255,255,0.4); letter-spacing: 0.04em; }
  .demo-switch { position: relative; display: inline-block; width: 32px; height: 18px; flex-shrink: 0; }
  .demo-switch input { opacity: 0; width: 0; height: 0; }
  .demo-slider {
    position: absolute; cursor: pointer; inset: 0;
    background: rgba(255,255,255,0.12); border-radius: 18px;
    transition: background 0.2s;
  }
  .demo-slider::before {
    content: ''; position: absolute;
    height: 12px; width: 12px; left: 3px; bottom: 3px;
    background: rgba(255,255,255,0.5); border-radius: 50%;
    transition: transform 0.2s;
  }
  .demo-switch input:checked + .demo-slider { background: #F59E0B; }
  .demo-switch input:checked + .demo-slider::before {
    transform: translateX(14px); background: #fff;
  }

  /* Demo banner */
  #demo-banner {
    display: none; align-items: center; gap: 6px;
    background: #F59E0B; color: #1a1100;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    padding: 3px 10px; border-radius: 20px; white-space: nowrap;
  }

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

  /* Plan picker (add a task to a chosen day) */
  .today-picker {
    position: absolute; background: var(--surface); border: 1.5px solid var(--border);
    border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.12);
    z-index: 50; width: 300px; display: none; padding: 8px;
  }
  .today-picker.open { display: block; }
  .today-picker input {
    width: 100%; padding: 6px 10px; border: 1.5px solid var(--border); border-radius: 6px;
    font-size: 12px; font-family: inherit; outline: none; background: var(--bg); color: var(--text);
    margin-bottom: 6px; box-sizing: border-box;
  }
  .today-picker-head { font-size: 11px; font-weight: 700; color: var(--text-muted);
                       text-transform: uppercase; letter-spacing: 0.5px; margin: 2px 2px 6px; }
  .today-picker-list { max-height: 240px; overflow-y: auto; }
  .today-picker-item { padding: 7px 8px; font-size: 12px; cursor: pointer; border-radius: 6px; }
  .today-picker-item:hover { background: var(--bg); }
  .today-picker-item .tpi-sheet { font-size: 10px; color: var(--text-muted); margin-top: 1px; }
  .today-picker-empty { padding: 10px; font-size: 12px; color: var(--text-muted); text-align: center; }

  /* ── Week board (Upcoming planner) ── */
  .week-nav { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
  .week-nav button {
    padding: 5px 11px; border: 1.5px solid var(--border); border-radius: 8px;
    background: var(--surface); color: var(--text); font-size: 13px; cursor: pointer; font-family: inherit;
  }
  .week-nav button:hover { background: var(--border); }
  .week-nav .week-label { font-size: 14px; font-weight: 700; color: var(--text); }
  .week-nav .week-hours { font-size: 12px; color: var(--text-muted); margin-left: auto; }

  .week-board {
    display: grid; grid-auto-flow: column; grid-auto-columns: minmax(190px, 1fr);
    gap: 10px; overflow-x: auto; padding-bottom: 8px; margin-bottom: 30px; align-items: start;
  }
  .day-col {
    background: var(--surface); border: 1.5px solid var(--border); border-radius: 12px;
    display: flex; flex-direction: column; min-height: 90px; max-height: 62vh;
    transition: border-color 0.15s, background 0.15s;
  }
  .day-col.is-today { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .day-col.drag-over { border-color: var(--accent); background: var(--accent-light); }
  .day-col-head {
    display: flex; align-items: baseline; gap: 6px; padding: 9px 11px 7px;
    border-bottom: 1px solid var(--border); position: sticky; top: 0;
  }
  .day-col-dow  { font-size: 12px; font-weight: 700; color: var(--text); }
  .day-col-date { font-size: 11px; color: var(--text-muted); }
  .day-col-hrs  { font-size: 10px; color: var(--text-muted); margin-left: auto; white-space: nowrap; }
  .day-col-add  {
    margin-left: 4px; background: none; border: none; cursor: pointer; color: var(--accent);
    font-size: 15px; font-weight: 700; line-height: 1; padding: 0 2px;
  }
  .day-col-body { overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 7px; flex: 1; }
  .day-empty { font-size: 11px; color: var(--text-muted); text-align: center; padding: 14px 6px; }

  .plan-card {
    background: var(--bg); border: 1px solid var(--border); border-left: 3px solid var(--accent);
    border-radius: 8px; padding: 7px 8px; font-size: 12px; position: relative;
  }
  .plan-card.done { opacity: 0.6; }
  .plan-card.done .plan-card-task { text-decoration: line-through; }
  .plan-card-top { display: flex; align-items: flex-start; gap: 5px; }
  .plan-rank {
    flex-shrink: 0; min-width: 16px; height: 16px; border-radius: 50%; background: var(--accent);
    color: #fff; font-size: 10px; font-weight: 700; display: flex; align-items: center;
    justify-content: center; margin-top: 1px; padding: 0 3px;
  }
  .plan-card-task { flex: 1; color: var(--text); line-height: 1.35; word-break: break-word; }
  .plan-card-proj { font-size: 10px; color: var(--text-muted); margin-top: 2px; }
  .plan-card-row { display: flex; align-items: center; gap: 4px; margin-top: 6px; }
  .plan-hours-input {
    width: 46px; padding: 2px 5px; border: 1px solid var(--border); border-radius: 5px;
    font-size: 11px; font-family: inherit; background: var(--surface); color: var(--text); outline: none;
  }
  .plan-hours-input:focus { border-color: var(--accent); }
  .plan-hours-label { font-size: 10px; color: var(--text-muted); }
  .plan-mini-btn {
    margin-left: auto; background: none; border: none; cursor: pointer; color: var(--text-muted);
    font-size: 12px; padding: 1px 4px; border-radius: 4px; line-height: 1;
  }
  .plan-mini-btn:hover { background: var(--border); color: var(--text); }
  .plan-mini-btn.up, .plan-mini-btn.down { margin-left: 0; font-size: 11px; }
  .plan-mini-btn.rm:hover { background: #FED7D7; color: #C53030; }
  .task-row[draggable="true"] { cursor: grab; }
  .task-row.dragging-task, .plan-card.dragging-task { opacity: 0.4; }

  /* ── Sync status pill ── */
  #sync-status {
    display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap; cursor: default; border: 1.5px solid transparent;
  }
  #sync-status .sync-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
  #sync-status.synced  { background: rgba(56,161,105,0.14); color: #2F855A; }
  #sync-status.synced  .sync-dot { background: #38A169; }
  #sync-status.offline { background: rgba(221,107,32,0.15); color: #C05621; }
  #sync-status.offline .sync-dot { background: #DD6B20; }
  #sync-status.syncing { background: rgba(66,153,225,0.15); color: #2B6CB0; }
  #sync-status.syncing .sync-dot { background: #4299E1; }
  #sync-status.localonly { background: var(--border); color: var(--text-muted); }
  #sync-status.localonly .sync-dot { background: var(--text-muted); }

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

  /* ── GarDone view ── */
  .gardone-wrap {
    padding: 0 0 60px;
  }
  .gardone-header {
    text-align: center;
    padding: 36px 24px 28px;
    border-bottom: 1px solid #C5B99A;
    margin-bottom: 36px;
    background: linear-gradient(to bottom, #F5EFE3, #EDE8DF);
    position: relative;
  }
  .gardone-title {
    font-family: 'Georgia', 'Times New Roman', serif;
    font-size: 38px;
    font-weight: 600;
    color: #2C1F14;
    letter-spacing: -0.01em;
    line-height: 1;
    margin-bottom: 6px;
  }
  .gardone-subtitle {
    font-family: 'Georgia', serif;
    font-size: 14px;
    font-style: italic;
    color: #7A6652;
    margin-bottom: 14px;
  }
  .gardone-stats-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: #5C4A35;
    background: rgba(255,255,255,0.55);
    border: 1px solid #C5B99A;
    border-radius: 40px;
    padding: 5px 18px;
    font-family: 'Georgia', serif;
  }
  .gardone-pill-sep { color: #B8A990; }
  .gardone-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
    gap: 24px;
    max-width: 1140px;
    margin: 0 auto;
    padding: 0 28px;
  }
  .gardone-specimen {
    position: relative;
    background: #FAF6EE;
    border: 1px solid #D4C5A9;
    border-radius: 4px;
    padding: 26px 24px 20px;
    box-shadow: 2px 3px 10px rgba(100,80,50,0.10);
    transition: box-shadow 0.18s ease, transform 0.18s ease;
  }
  .gardone-specimen:hover {
    box-shadow: 4px 6px 22px rgba(100,80,50,0.17);
    transform: translateY(-2px);
  }
  .gardone-specimen::before {
    content: '';
    position: absolute; top: 9px; left: 50%; transform: translateX(-50%);
    width: 7px; height: 7px; border-radius: 50%;
    background: #C5B99A;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.18);
  }
  .gardone-inner { display: flex; gap: 20px; align-items: flex-start; }
  .gardone-flower-col {
    flex-shrink: 0;
    display: flex; flex-direction: column; align-items: center; gap: 5px;
  }
  .gardone-flower-ring {
    padding: 9px; border: 1px solid; border-radius: 50%;
    background: rgba(255,255,255,0.6);
    display: flex; align-items: center; justify-content: center;
  }
  .gardone-notes-col { flex: 1; min-width: 0; }
  .gardone-spec-header {
    display: flex; align-items: baseline;
    justify-content: space-between; gap: 10px;
    padding-bottom: 8px; border-bottom: 1px solid;
    margin-bottom: 12px;
  }
  .gardone-spec-name {
    font-family: 'Georgia', serif;
    font-size: 18px; font-weight: 600; color: #2C1F14; flex: 1;
  }
  .gardone-spec-stats {
    font-family: 'Georgia', serif;
    font-size: 11px; font-style: italic; color: #8C7A65; white-space: nowrap;
  }
  .gardone-task-list { list-style: none; display: flex; flex-direction: column; gap: 5px; }
  .gardone-task-entry {
    display: flex; align-items: baseline; gap: 6px;
    font-family: 'Georgia', serif; font-size: 13.5px; line-height: 1.45; color: #3D2B1F;
    padding-bottom: 5px; border-bottom: 1px solid rgba(180,160,120,0.18);
  }
  .gardone-task-entry:last-child { border-bottom: none; padding-bottom: 0; }
  .gardone-tick { font-size: 10px; flex-shrink: 0; margin-top: 2px; }
  .gardone-task-name { flex: 1; }
  .gardone-task-date {
    font-size: 11px; font-style: italic; color: #A08060; flex-shrink: 0; white-space: nowrap;
  }
  .gardone-stamp {
    position: absolute; bottom: 12px; right: 14px;
    font-size: 9px; font-weight: 600; letter-spacing: 0.2em;
    border: 1px solid; border-radius: 2px; padding: 2px 5px;
    opacity: 0.22; transform: rotate(-2deg);
  }
  .gardone-empty {
    text-align: center; font-family: 'Georgia', serif;
    font-style: italic; font-size: 17px; color: #A08060;
    padding: 60px 0;
  }

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
  <div id="literature-btn" onclick="selectView('literature')">📚 Literature</div>
  <div id="collabs-btn" onclick="selectView('collaborators')">👥 Collaborators</div>
  <div id="stats-btn"         onclick="selectView('stats')">📊 Stats</div>
  <div id="gardone-btn"       onclick="selectView('gardone')">🌸 GarDone</div>
  <div id="procrastinate-btn" onclick="selectView('procrastinate')">🐍 Procrastinate</div>
  <div class="sidebar-sep"></div>
  <div id="sheet-list"></div>
  <div id="sidebar-footer">
    <button id="new-project-btn" onclick="openNewProjectModal()">+ New Project</button>
    <div class="demo-toggle-wrap">
      <span class="demo-toggle-label">Demo Mode</span>
      <label class="demo-switch">
        <input type="checkbox" id="demo-toggle-cb" onchange="toggleDemoMode(this.checked)">
        <span class="demo-slider"></span>
      </label>
    </div>
  </div>
  <div id="sidebar-resize"></div>
</div>

<div id="main">
  <div id="topbar">
    <h2 id="topbar-title">All Projects</h2>
    <div id="topbar-right">
      <div id="demo-banner">📸 DEMO</div>
      <span id="sync-status" class="localonly" title="Sync status" style="display:none">
        <span class="sync-dot"></span><span id="sync-status-text">Local</span>
      </span>
      <button id="refresh-btn" onclick="syncAll()">↻ Sync</button>
      <button id="add-btn" onclick="openAddModal()">+ Add Task</button>
    </div>
    <div id="filter-area">
      <button class="filter-btn active" data-s="all" onclick="setFilter('all')">All</button>
      <button class="filter-btn" data-s="Pending" onclick="setFilter('Pending')">Pending</button>
      <button class="filter-btn" data-s="In Progress" onclick="setFilter('In Progress')">In Progress</button>
      <button class="filter-btn" data-s="Not Started" onclick="setFilter('Not Started')">Not Started</button>
      <button class="filter-btn" data-s="Completed" onclick="setFilter('Completed')">Completed</button>
      <input id="search-input" type="search" placeholder="🔍 Search tasks…" oninput="onSearch()" style="display:none;margin-left:auto">
      <button id="export-btn" onclick="exportCSV()" style="display:none">⬇ Export</button>
    </div>
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
      <label>Title <span style="font-weight:400;text-transform:none">(optional)</span></label>
      <input id="n-title" type="text" placeholder="Note title…">
    </div>
    <div class="field">
      <label>Note</label>
      <div class="note-editor-toolbar">
        <button type="button" class="net-btn" onmousedown="event.preventDefault();execFmt('bold')" title="Bold"><b>B</b></button>
        <button type="button" class="net-btn" onmousedown="event.preventDefault();execFmt('italic')" title="Italic"><i>I</i></button>
        <button type="button" class="net-btn" onmousedown="event.preventDefault();execFmt('underline')" title="Underline"><u>U</u></button>
        <div class="net-sep"></div>
        <button type="button" class="net-btn" onmousedown="event.preventDefault();execFmt('insertUnorderedList')" title="Bullet list">• List</button>
        <button type="button" class="net-btn" onmousedown="event.preventDefault();execFmt('insertOrderedList')" title="Numbered list">1. List</button>
        <div class="net-sep"></div>
        <select id="n-font" class="net-select" onchange="applyNoteFont()" title="Font family">
          <option value="">Default</option>
          <option value="Georgia, serif">Georgia</option>
          <option value="'Courier New', monospace">Monospace</option>
          <option value="'Trebuchet MS', sans-serif">Trebuchet</option>
          <option value="Palatino, serif">Palatino</option>
        </select>
        <select id="n-size" class="net-select" onchange="applyNoteFont()" title="Font size">
          <option value="12">12</option>
          <option value="13">13</option>
          <option value="14" selected>14</option>
          <option value="16">16</option>
          <option value="18">18</option>
        </select>
      </div>
      <div id="n-editor" contenteditable="true" spellcheck="true" data-placeholder="Write your note…"></div>
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

<!-- Literature modal -->
<div class="modal-overlay" id="lit-modal">
  <div class="modal">
    <h3 id="lit-modal-title">Add Reference</h3>
    <div class="field">
      <label>Project</label>
      <select id="l-project"></select>
    </div>
    <div class="field">
      <label>Title</label>
      <input id="l-title" type="text" placeholder="Paper title…">
    </div>
    <div class="field">
      <label>Link <span style="font-weight:400;text-transform:none">(optional)</span></label>
      <input id="l-link" type="url" placeholder="https://…">
    </div>
    <div class="field" style="display:flex;gap:14px">
      <div style="flex:1">
        <label>Authors <span style="font-weight:400;text-transform:none">(optional)</span></label>
        <input id="l-authors" type="text" placeholder="Smith, J., Doe, A.">
      </div>
      <div style="flex:0 0 90px">
        <label>Year <span style="font-weight:400;text-transform:none">(optional)</span></label>
        <input id="l-year" type="text" placeholder="2026">
      </div>
    </div>
    <div class="field">
      <label>Notes <span style="font-weight:400;text-transform:none">(optional)</span></label>
      <textarea id="l-notes" rows="3" placeholder="Why this matters, key takeaway…"></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeLitModal()">Cancel</button>
      <button class="btn-save" onclick="saveLit()">Save</button>
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

<!-- Plan picker: add a task to a chosen day -->
<div class="today-picker" id="today-picker" onclick="event.stopPropagation()">
  <div class="today-picker-head" id="today-picker-head">Add to day</div>
  <input type="text" id="today-picker-search" placeholder="Search tasks…" oninput="renderTodayPickerList()">
  <div class="today-picker-list" id="today-picker-list"></div>
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
let _projectOrder = JSON.parse(localStorage.getItem('projectOrder') || 'null');
function orderedSheets() {
  if (!_projectOrder) return allSheets;
  return [...allSheets].sort((a, b) => {
    const ia = _projectOrder.indexOf(a.name), ib = _projectOrder.indexOf(b.name);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1; if (ib === -1) return -1;
    return ia - ib;
  });
}
let allTasks   = {};
let allNotes   = [];
let allCollabs = [];
let allLiterature = [];
let allPlan = [];          // rows from /api/plan: {row, project, task_row, day, order, hours, ...}
let _weekOffset = 0;       // 0 = current week, -1 = last week, +1 = next week
let _planPickerDay = null; // ISO date the picker will add a task to
let _syncPollTimer = null;
let activeSheet  = null;
let activeFilter = 'all';
let viewMode     = 'tasks';
let editTarget   = null;
let modalMode    = 'add';
let noteEditRow  = null;
let selectedNoteColor = '#FFF9C4';
let litEditRow   = null;
let searchQuery = '';
const selectedTasks  = new Set();  // "sheet::row"
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

// ── Demo mode ─────────────────────────────────────────────────────────────

const DEMO_DATA = {
  sheets: [
    {name:"Research Overview",      active:11},
    {name:"Reproducibility Study",  active:2},
    {name:"Interdisciplinary Grant",active:3},
    {name:"Science Policy Brief",   active:3},
    {name:"K-12 AI Curriculum",     active:3},
    {name:"Paper Discovery Tool",   active:0},
    {name:"Open Science Writing",   active:3},
    {name:"Science Software Study", active:0},
    {name:"Citation Analysis",      active:1},
    {name:"Survey Design",          active:1},
    {name:"Diversity in Research",  active:4},
    {name:"Longitudinal Study",     active:2},
    {name:"Interview Methods",      active:3},
    {name:"Academic Publishing",    active:2},
  ],
  tasks: {
    "Research Overview": [
      {row:2,  deadline:"15 Jun 2026", task:"Draft introduction section",     hours:"3",  status:"Completed",   completed_date:"12 Mar 2026", assignee:"Alex"},
      {row:3,  deadline:"",            task:"Set up data pipeline",            hours:"5",  status:"Completed",   completed_date:"15 Feb 2026", assignee:"Jordan"},
      {row:4,  deadline:"",            task:"Literature review — first pass",  hours:"8",  status:"Completed",   completed_date:"20 Jan 2026", assignee:""},
      {row:5,  deadline:"",            task:"Stakeholder interviews",          hours:"4",  status:"Completed",   completed_date:"5 Mar 2026",  assignee:"Priya"},
      {row:6,  deadline:"",            task:"IRB submission",                  hours:"2",  status:"Completed",   completed_date:"28 Feb 2026", assignee:""},
      {row:7,  deadline:"",            task:"Pilot study analysis",            hours:"6",  status:"Completed",   completed_date:"1 Apr 2026",  assignee:"Jordan"},
      {row:8,  deadline:"",            task:"Submit abstract to conference",   hours:"1",  status:"Completed",   completed_date:"10 Apr 2026", assignee:""},
      {row:9,  deadline:"20 May 2026", task:"Run regression models",           hours:"4",  status:"Pending",     completed_date:"", assignee:"Alex"},
      {row:10, deadline:"1 Jun 2026",  task:"Write methods section",           hours:"5",  status:"Pending",     completed_date:"", assignee:""},
      {row:11, deadline:"15 Jun 2026", task:"Peer review response",            hours:"3",  status:"Pending",     completed_date:"", assignee:"Priya"},
      {row:12, deadline:"25 Jun 2026", task:"Final proofreading",              hours:"2",  status:"Pending",     completed_date:"", assignee:""},
      {row:13, deadline:"10 Jul 2026", task:"Write discussion section",        hours:"4",  status:"Not Started", completed_date:"", assignee:""},
      {row:14, deadline:"15 Jul 2026", task:"Create figures and tables",       hours:"3",  status:"Not Started", completed_date:"", assignee:"Jordan"},
      {row:15, deadline:"1 Aug 2026",  task:"External reviewer follow-up",     hours:"2",  status:"Not Started", completed_date:"", assignee:""},
      {row:16, deadline:"15 Aug 2026", task:"Camera-ready submission",         hours:"1",  status:"Not Started", completed_date:"", assignee:""},
      {row:17, deadline:"20 Aug 2026", task:"Prepare conference talk slides",  hours:"3",  status:"Not Started", completed_date:"", assignee:"Alex"},
      {row:18, deadline:"25 Aug 2026", task:"Record demo video",               hours:"2",  status:"Not Started", completed_date:"", assignee:""},
      {row:19, deadline:"30 May 2026", task:"Annotate dataset",                hours:"10", status:"In Progress", completed_date:"", assignee:"Priya, Jordan"},
    ],
    "Reproducibility Study": [
      {row:2, deadline:"30 May 2026", task:"Replicate baseline experiments",      hours:"8", status:"Pending",     completed_date:"", assignee:""},
      {row:3, deadline:"15 Jun 2026", task:"Document reproducibility checklist",  hours:"2", status:"Not Started", completed_date:"", assignee:""},
    ],
    "Interdisciplinary Grant": [
      {row:2, deadline:"15 Jul 2026", task:"Draft specific aims page",    hours:"4", status:"Not Started", completed_date:"", assignee:""},
      {row:3, deadline:"20 Jul 2026", task:"Prepare budget justification", hours:"3", status:"Not Started", completed_date:"", assignee:"Alex"},
      {row:4, deadline:"25 Jul 2026", task:"Collect letters of support",  hours:"1", status:"Not Started", completed_date:"", assignee:""},
    ],
    "Science Policy Brief": [
      {row:2, deadline:"15 May 2026", task:"Outline policy brief structure",  hours:"2", status:"Pending", completed_date:"", assignee:""},
      {row:3, deadline:"1 Jun 2026",  task:"Gather evidence and citations",   hours:"4", status:"Pending", completed_date:"", assignee:"Jordan"},
      {row:4, deadline:"20 Jun 2026", task:"Draft executive summary",         hours:"3", status:"Pending", completed_date:"", assignee:""},
    ],
    "K-12 AI Curriculum": [
      {row:2, deadline:"",            task:"Design lesson plan framework",   hours:"4", status:"Completed", completed_date:"1 Mar 2026",  assignee:"Priya"},
      {row:3, deadline:"",            task:"Develop teacher training guide", hours:"6", status:"Completed", completed_date:"15 Mar 2026", assignee:""},
      {row:4, deadline:"1 Jun 2026",  task:"Pilot curriculum with schools",  hours:"8", status:"Pending",   completed_date:"", assignee:"Priya"},
      {row:5, deadline:"15 Jun 2026", task:"Revise based on pilot feedback", hours:"4", status:"Pending",   completed_date:"", assignee:""},
      {row:6, deadline:"30 Jun 2026", task:"Submit grant deliverable",       hours:"1", status:"Pending",   completed_date:"", assignee:""},
    ],
    "Paper Discovery Tool": [
      {row:2, deadline:"", task:"Build keyword extraction module", hours:"6", status:"Completed", completed_date:"10 Jan 2026", assignee:"Jordan"},
      {row:3, deadline:"", task:"Test on benchmark datasets",      hours:"4", status:"Completed", completed_date:"20 Jan 2026", assignee:""},
      {row:4, deadline:"", task:"Write technical report",          hours:"3", status:"Completed", completed_date:"1 Feb 2026",  assignee:""},
    ],
    "Open Science Writing": [
      {row:2, deadline:"1 May 2026",  task:"Draft open science statement", hours:"2", status:"Pending", completed_date:"", assignee:""},
      {row:3, deadline:"15 May 2026", task:"Document code repository",     hours:"3", status:"Pending", completed_date:"", assignee:"Alex"},
      {row:4, deadline:"30 May 2026", task:"Publish preprint",             hours:"1", status:"Pending", completed_date:"", assignee:""},
    ],
    "Science Software Study": [],
    "Citation Analysis": [
      {row:2, deadline:"",            task:"Collect citation network data", hours:"3", status:"Completed", completed_date:"5 Feb 2026",  assignee:""},
      {row:3, deadline:"15 May 2026", task:"Run network analysis",          hours:"5", status:"Pending",   completed_date:"", assignee:"Jordan"},
    ],
    "Survey Design": [
      {row:2, deadline:"1 May 2026", task:"Finalize survey instrument", hours:"4", status:"In Progress", completed_date:"", assignee:"Alex"},
    ],
    "Diversity in Research": [
      {row:2, deadline:"15 May 2026", task:"Code interview transcripts",  hours:"8", status:"Pending", completed_date:"", assignee:"Priya"},
      {row:3, deadline:"1 Jun 2026",  task:"Identify emerging themes",    hours:"4", status:"Pending", completed_date:"", assignee:""},
      {row:4, deadline:"15 Jun 2026", task:"Draft findings section",      hours:"3", status:"Pending", completed_date:"", assignee:""},
      {row:5, deadline:"30 Jun 2026", task:"Submit to journal",           hours:"1", status:"Pending", completed_date:"", assignee:""},
    ],
    "Longitudinal Study": [
      {row:2, deadline:"1 Jul 2026",  task:"Design follow-up survey",          hours:"3", status:"Not Started", completed_date:"", assignee:""},
      {row:3, deadline:"15 Jul 2026", task:"Coordinate with research sites",    hours:"2", status:"Not Started", completed_date:"", assignee:"Alex"},
    ],
    "Interview Methods": [
      {row:2, deadline:"",            task:"Recruit interview participants", hours:"2",  status:"Completed",   completed_date:"1 Mar 2026",  assignee:""},
      {row:3, deadline:"15 May 2026", task:"Conduct interviews",             hours:"12", status:"Pending",     completed_date:"", assignee:"Priya"},
      {row:4, deadline:"30 May 2026", task:"Transcribe recordings",          hours:"6",  status:"Pending",     completed_date:"", assignee:"Jordan"},
      {row:5, deadline:"15 Jun 2026", task:"Member check with participants", hours:"3",  status:"Not Started", completed_date:"", assignee:""},
    ],
    "Academic Publishing": [
      {row:2, deadline:"1 Jun 2026", task:"Map predatory journal landscape", hours:"4", status:"Not Started", completed_date:"", assignee:""},
      {row:3, deadline:"1 Jul 2026", task:"Draft white paper",               hours:"6", status:"Not Started", completed_date:"", assignee:""},
    ],
  },
  notes: [
    {row:2, project:"Research Overview",  note:"Align methods section with RQ3 — reviewers flagged this last round. Check framing in §3.2.", importance:"High",   purpose:"Writing",  color:"#FFF9C4", created:"15 Apr 2026", modified:"15 Apr 2026"},
    {row:3, project:"K-12 AI Curriculum", note:"Teacher feedback from pilot was very positive — use direct quotes in the grant deliverable.", importance:"Medium", purpose:"Planning", color:"#C8E6C9", created:"20 Mar 2026", modified:"20 Mar 2026"},
  ],
  collabs: [
    {row:2,  project:"Research Overview",       name:"Alex Chen",       role:"Researcher"},
    {row:3,  project:"Research Overview",       name:"Jordan Lee",      role:"Data Analyst"},
    {row:4,  project:"Research Overview",       name:"Priya Sharma",    role:"Co-investigator"},
    {row:5,  project:"Research Overview",       name:"Dr. Reyes",       role:"Advisor"},
    {row:6,  project:"Research Overview",       name:"Sam Patel",       role:"RA"},
    {row:7,  project:"Research Overview",       name:"Mia Torres",      role:"Statistician"},
    {row:8,  project:"Reproducibility Study",   name:"Alex Chen",       role:"Researcher"},
    {row:9,  project:"Reproducibility Study",   name:"Jordan Lee",      role:"Data Analyst"},
    {row:10, project:"Reproducibility Study",   name:"Fatima Hassan",   role:"Methods Lead"},
    {row:11, project:"Interdisciplinary Grant", name:"Alex Chen",       role:"PI"},
    {row:12, project:"Interdisciplinary Grant", name:"Dr. Reyes",       role:"Co-PI"},
    {row:13, project:"Interdisciplinary Grant", name:"Sam Patel",       role:"Grant Writer"},
    {row:14, project:"Interdisciplinary Grant", name:"Lin Wei",         role:"Budget Manager"},
    {row:15, project:"Science Policy Brief",    name:"Jordan Lee",      role:"Lead Author"},
    {row:16, project:"Science Policy Brief",    name:"Priya Sharma",    role:"Policy Analyst"},
    {row:17, project:"Science Policy Brief",    name:"Dr. Osei",        role:"External Reviewer"},
    {row:18, project:"K-12 AI Curriculum",      name:"Priya Sharma",    role:"Lead"},
    {row:19, project:"K-12 AI Curriculum",      name:"Marcus Brown",    role:"Curriculum Designer"},
    {row:20, project:"K-12 AI Curriculum",      name:"Elena Vasquez",   role:"Teacher Liaison"},
    {row:21, project:"K-12 AI Curriculum",      name:"Sam Patel",       role:"RA"},
    {row:22, project:"K-12 AI Curriculum",      name:"Jordan Lee",      role:"Evaluator"},
    {row:23, project:"Paper Discovery Tool",    name:"Jordan Lee",      role:"Developer"},
    {row:24, project:"Paper Discovery Tool",    name:"Alex Chen",       role:"Research Lead"},
    {row:25, project:"Paper Discovery Tool",    name:"Fatima Hassan",   role:"Tester"},
    {row:26, project:"Open Science Writing",    name:"Alex Chen",       role:"Lead"},
    {row:27, project:"Open Science Writing",    name:"Mia Torres",      role:"Technical Writer"},
    {row:28, project:"Citation Analysis",       name:"Jordan Lee",      role:"Data Lead"},
    {row:29, project:"Citation Analysis",       name:"Lin Wei",         role:"Analyst"},
    {row:30, project:"Citation Analysis",       name:"Dr. Reyes",       role:"Supervisor"},
    {row:31, project:"Citation Analysis",       name:"Sam Patel",       role:"RA"},
    {row:32, project:"Survey Design",           name:"Alex Chen",       role:"Lead"},
    {row:33, project:"Survey Design",           name:"Priya Sharma",    role:"Survey Expert"},
    {row:34, project:"Diversity in Research",   name:"Priya Sharma",    role:"Lead"},
    {row:35, project:"Diversity in Research",   name:"Marcus Brown",    role:"Qualitative Analyst"},
    {row:36, project:"Diversity in Research",   name:"Elena Vasquez",   role:"Field Researcher"},
    {row:37, project:"Diversity in Research",   name:"Jordan Lee",      role:"Data Support"},
    {row:38, project:"Longitudinal Study",      name:"Alex Chen",       role:"PI"},
    {row:39, project:"Longitudinal Study",      name:"Dr. Reyes",       role:"Advisor"},
    {row:40, project:"Longitudinal Study",      name:"Sam Patel",       role:"Coordinator"},
    {row:41, project:"Interview Methods",       name:"Priya Sharma",    role:"Lead"},
    {row:42, project:"Interview Methods",       name:"Jordan Lee",      role:"Interviewer"},
    {row:43, project:"Interview Methods",       name:"Sam Patel",       role:"Transcriber"},
    {row:44, project:"Interview Methods",       name:"Lin Wei",         role:"Analyst"},
    {row:45, project:"Interdisciplinary Grant", name:"Mia Torres",      role:"Writer"},
    {row:46, project:"Science Policy Brief",    name:"Sam Patel",       role:"Research Assistant"},
    {row:47, project:"Diversity in Research",   name:"Lin Wei",         role:"Data Analyst"},
    {row:48, project:"Open Science Writing",    name:"Priya Sharma",    role:"Reviewer"},
  ],
  literature: [
    {row:2, project:"Research Overview", title:"Attention Is All You Need", link:"https://arxiv.org/abs/1706.03762",
     authors:"Vaswani, A. et al.", year:"2017", notes:"Foundational transformer architecture — cite in related work.",
     created:"10 Mar 2026", modified:"10 Mar 2026"},
    {row:3, project:"Citation Analysis", title:"The Leiden Manifesto for research metrics", link:"https://www.nature.com/articles/520429a",
     authors:"Hicks, D. et al.", year:"2015", notes:"Good framing for our metrics critique section.",
     created:"2 Apr 2026", modified:"2 Apr 2026"},
  ],
  plan: [],
};

let demoMode = localStorage.getItem('demoMode') === '1';

async function toggleDemoMode(on) {
  demoMode = on;
  localStorage.setItem('demoMode', on ? '1' : '0');
  document.getElementById('demo-banner').style.display = on ? 'flex' : 'none';
  // Reset project selection (names differ between real and demo) but keep current view
  activeSheet = null;
  if (viewMode === 'tasks') document.getElementById('topbar-title').textContent = 'All Projects';
  await Promise.all([loadSheets(), loadTasks(true), loadNotes(), loadCollabs(), loadLiterature(), loadPlan()]);
  renderContent();
  refreshSyncStatus();
}

function guardDemo() {
  if (!demoMode) return false;
  return true;  // silently block mutations in demo mode
}

async function init() {
  applyTheme(localStorage.getItem('theme') || 'classic');
  if (demoMode) {
    document.getElementById('demo-toggle-cb').checked = true;
    document.getElementById('demo-banner').style.display = 'flex';
  }
  document.getElementById('content').innerHTML = '<div class="spinner">Loading tasks…</div>';
  if (!demoMode) {
    const status = await fetch('/api/status').then(r => r.json()).catch(() => ({}));
    if (status.local_mode) {
      document.getElementById('local-mode-badge').style.display = '';
      document.getElementById('refresh-btn').title = 'Reload from local storage';
    }
  }
  await Promise.all([loadSheets(), loadTasks(), loadNotes(), loadCollabs(), loadLiterature(), loadPlan()]);
  refreshSyncStatus();
  startSyncPolling();
}

async function loadSheets() {
  if (demoMode) {
    allSheets = DEMO_DATA.sheets.map(s => ({...s}));
    renderSidebar(); return;
  }
  const res = await fetch('/api/sheets');
  allSheets = await res.json();
  renderSidebar();
}

async function loadTasks(silent = false) {
  if (!silent) document.getElementById('content').innerHTML = '<div class="spinner">Loading…</div>';
  if (demoMode) {
    allTasks = Object.fromEntries(
      Object.entries(DEMO_DATA.tasks).map(([k,v]) => [k, v.map(t => ({...t}))]));
    renderContent(); return;
  }
  const res = await fetch('/api/tasks');
  allTasks = await res.json();
  renderContent();
}

async function loadNotes() {
  if (demoMode) { allNotes = DEMO_DATA.notes.map(n => ({...n})); return; }
  const res = await fetch('/api/notes');
  allNotes = await res.json();
}

async function loadCollabs() {
  if (demoMode) { allCollabs = DEMO_DATA.collabs.map(c => ({...c})); return; }
  const res = await fetch('/api/collaborators');
  allCollabs = await res.json();
}

async function loadLiterature() {
  if (demoMode) { allLiterature = DEMO_DATA.literature.map(l => ({...l})); return; }
  const res = await fetch('/api/literature');
  allLiterature = await res.json();
}

async function loadPlan() {
  if (demoMode) { allPlan = (DEMO_DATA.plan || []).map(p => ({...p})); return; }
  const res = await fetch('/api/plan');
  allPlan = await res.json();
}

async function syncAll() {
  if (demoMode) {
    await Promise.all([loadSheets(), loadTasks(true), loadNotes(), loadCollabs(), loadLiterature(), loadPlan()]);
    renderContent(); return;
  }
  const btn = document.getElementById('refresh-btn');
  btn.textContent = '↻ Syncing…';
  btn.disabled = true;
  setSyncPill('syncing');
  try {
    await fetch('/api/sync', { method: 'POST' });
    await Promise.all([loadSheets(), loadTasks(true), loadNotes(), loadCollabs(), loadLiterature(), loadPlan()]);
    renderContent();
  } finally {
    btn.textContent = '↻ Sync';
    btn.disabled = false;
    refreshSyncStatus();
  }
}

// ── Sync status pill ──────────────────────────────────────────────────────

function setSyncPill(state, text) {
  const pill = document.getElementById('sync-status');
  if (!pill) return;
  pill.style.display = '';
  pill.className = state;
  document.getElementById('sync-status-text').textContent = text || state;
}

async function refreshSyncStatus() {
  if (demoMode) { const p = document.getElementById('sync-status'); if (p) p.style.display = 'none'; return; }
  let st;
  try { st = await fetch('/api/status').then(r => r.json()); }
  catch (e) { setSyncPill('offline', 'Offline'); return; }
  if (!st.remote_enabled) { setSyncPill('localonly', 'Local only'); return; }
  if (st.pending > 0) {
    setSyncPill('offline', `Offline · ${st.pending} pending`);
  } else if (!st.online) {
    setSyncPill('offline', 'Offline');
  } else {
    setSyncPill('synced', 'Synced');
  }
}

function startSyncPolling() {
  if (_syncPollTimer) clearInterval(_syncPollTimer);
  _syncPollTimer = setInterval(() => {
    refreshSyncStatus();
    // If changes are queued, POST /api/sync nudges the server to drain them now.
    const pill = document.getElementById('sync-status');
    if (pill && pill.classList.contains('offline')) {
      fetch('/api/sync', { method: 'POST' }).then(() => refreshSyncStatus()).catch(() => {});
    }
  }, 15000);
}

// ── Sidebar ───────────────────────────────────────────────────────────────

function setSidebarActive(id) {
  ['show-all','upcoming-btn','notes-btn','literature-btn','collabs-btn','stats-btn','gardone-btn','procrastinate-btn'].forEach(i =>
    document.getElementById(i).className = (i === id ? 'active' : ''));
  document.querySelectorAll('.sheet-item').forEach(el => el.classList.remove('active'));
}

function updateTopbar() {
  // Filter pills apply only to the task list; the Upcoming planner ignores them.
  document.getElementById('filter-area').style.display = viewMode === 'tasks' ? '' : 'none';
  const searchEl = document.getElementById('search-input');
  const exportEl = document.getElementById('export-btn');
  if (searchEl) searchEl.style.display = viewMode === 'tasks' ? '' : 'none';
  if (exportEl) exportEl.style.display = viewMode === 'tasks' ? '' : 'none';
  const btn = document.getElementById('add-btn');
  if (viewMode === 'procrastinate' || viewMode === 'stats' || viewMode === 'gardone') {
    btn.style.display = 'none';
  } else {
    btn.style.display = '';
    if (viewMode === 'notes') {
      btn.textContent = '+ Add Note';
      btn.onclick = () => openNoteModal(null);
    } else if (viewMode === 'literature') {
      btn.textContent = '+ Add Reference';
      btn.onclick = () => openLitModal(null);
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
  let _dragSrc = null;
  orderedSheets().forEach(s => {
    totalActive += s.active;
    const div = document.createElement('div');
    div.className = 'sheet-item' + (activeSheet === s.name ? ' active' : '');
    div.dataset.name = s.name;
    div.draggable = true;
    div.innerHTML = `<span class="drag-handle">⠿</span><span>${escHtml(s.name)}</span><span class="badge">${s.active}</span>`;
    div.onclick = () => selectSheet(s.name);
    div.addEventListener('dragstart', e => {
      _dragSrc = div; e.dataTransfer.effectAllowed = 'move';
      setTimeout(() => div.classList.add('dragging'), 0);
    });
    div.addEventListener('dragend', () => div.classList.remove('dragging'));
    div.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; });
    div.addEventListener('dragenter', e => { e.preventDefault(); div.classList.add('drag-over'); });
    div.addEventListener('dragleave', () => div.classList.remove('drag-over'));
    div.addEventListener('drop', e => {
      e.preventDefault(); div.classList.remove('drag-over');
      if (!_dragSrc || _dragSrc === div) return;
      const items = [...list.querySelectorAll('.sheet-item')];
      const si = items.indexOf(_dragSrc), di = items.indexOf(div);
      if (si < di) list.insertBefore(_dragSrc, div.nextSibling);
      else         list.insertBefore(_dragSrc, div);
      _projectOrder = [...list.querySelectorAll('.sheet-item')].map(el => el.dataset.name);
      localStorage.setItem('projectOrder', JSON.stringify(_projectOrder));
    });
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
    { notes:'Notes', literature:'📚 Literature', collaborators:'Collaborators', procrastinate:'🐍 Procrastinate', stats:'📊 Stats', gardone:'🌸 GarDone' }[mode] || mode;
  const sid = { notes:'notes-btn', literature:'literature-btn', collaborators:'collabs-btn', procrastinate:'procrastinate-btn', stats:'stats-btn', gardone:'gardone-btn' };
  setSidebarActive(sid[mode] || null);
  updateTopbar();
  renderContent();
}

function renderContent() {
  updateTopbar();
  if (viewMode !== 'procrastinate') cleanupProcrastinate();
  if (viewMode === 'upcoming')           renderUpcoming();
  else if (viewMode === 'notes')         renderNotes();
  else if (viewMode === 'literature')    renderLiterature();
  else if (viewMode === 'collaborators') renderCollaborators();
  else if (viewMode === 'procrastinate') renderProcrastinate();
  else if (viewMode === 'stats')         renderStats();
  else if (viewMode === 'gardone')       renderGarDone();
  else                                   renderTasks();
}

// ── Tasks ─────────────────────────────────────────────────────────────────

function noteProjectCard(n) {
  const p = parseNote(n.note);
  const plainText = p.title ? p.title + (p.body ? ' — ' + (new DOMParser().parseFromString(p.body,'text/html').body.textContent||'') : '') : (new DOMParser().parseFromString(p.body,'text/html').body.textContent||'');
  const preview = plainText.length > 130 ? plainText.slice(0,130) + '…' : plainText;
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
  const days = weekDates(_weekOffset);
  const todayIsoStr = todayIso();

  const byDay = {};
  allPlan.forEach(p => { (byDay[p.day] = byDay[p.day] || []).push(p); });

  let weekTotal = 0;
  const cols = days.map((d, i) => {
    const iso = isoDay(d);
    const entries = (byDay[iso] || []).slice().sort((a, b) => (a.order - b.order) || (a.row - b.row));
    let dayHours = 0;
    const cards = entries.map((p, idx) => {
      const h = parseFloat(p.hours); if (!isNaN(h)) dayHours += h;
      return planCardHtml(p, idx, entries.length);
    }).join('');
    weekTotal += dayHours;
    const dateLabel = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    const dowLabel = (iso === todayIsoStr) ? 'Today' : d.toLocaleDateString('en-GB', { weekday: 'short' });
    return `<div class="day-col ${iso === todayIsoStr ? 'is-today' : ''}" data-day="${iso}"
        ondragover="onDayDragOver(event)" ondragleave="event.currentTarget.classList.remove('drag-over')"
        ondrop="onDayDrop(event,'${iso}')">
      <div class="day-col-head">
        <span class="day-col-dow">${dowLabel}</span>
        <span class="day-col-date">${escHtml(dateLabel)}</span>
        ${dayHours ? `<span class="day-col-hrs">${(+dayHours.toFixed(2))}h</span>` : ''}
        <button class="day-col-add" title="Add a task to this day" onclick="openDayPicker(event,'${iso}')">+</button>
      </div>
      <div class="day-col-body">
        ${cards || `<div class="day-empty">Drop a task here<br>or click +</div>`}
      </div>
    </div>`;
  }).join('');

  const range = days[0].toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) +
                ' – ' + days[6].toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const nav = `<div class="week-nav">
      <button onclick="changeWeek(-1)">‹ Prev</button>
      <span class="week-label">${escHtml(range)}</span>
      <button onclick="changeWeek(1)">Next ›</button>
      ${_weekOffset !== 0 ? `<button onclick="gotoThisWeek()">Today</button>` : ''}
      ${weekTotal ? `<span class="week-hours">Planned: ${(+weekTotal.toFixed(2))}h</span>` : ''}
    </div>`;

  content.innerHTML = nav + `<div class="week-board">${cols}</div>` + upcomingBacklogHtml();
}

function planCardHtml(p, idx, total) {
  const t = taskFor(p.project, p.task_row);
  const done = t && t.status === 'Completed';
  const taskText = t ? t.task : '(task no longer exists)';
  const sub = [p.project, t && t.deadline ? '📅 ' + t.deadline : '', t ? t.status : '']
                .filter(Boolean).join(' · ');
  const hoursVal = (p.hours != null ? String(p.hours) : '');
  return `<div class="plan-card ${done ? 'done' : ''}" draggable="true" data-planrow="${p.row}"
      ondragstart="onPlanCardDragStart(event,${p.row})" ondragend="event.currentTarget.classList.remove('dragging-task')">
    <div class="plan-card-top">
      <span class="plan-rank">${idx + 1}</span>
      <div style="flex:1;min-width:0">
        <div class="plan-card-task">${escHtml(taskText)}</div>
        <div class="plan-card-proj">${escHtml(sub)}</div>
      </div>
      <button class="plan-mini-btn rm" title="Remove from plan" onclick="removePlan(${p.row})">✕</button>
    </div>
    <div class="plan-card-row">
      <button class="plan-mini-btn up"   title="Higher priority" ${idx === 0 ? 'disabled' : ''} onclick="movePlanRank(${p.row},-1)">▲</button>
      <button class="plan-mini-btn down" title="Lower priority" ${idx === total - 1 ? 'disabled' : ''} onclick="movePlanRank(${p.row},1)">▼</button>
      <input class="plan-hours-input" type="number" min="0" step="0.5" value="${escHtml(hoursVal)}"
        title="Estimated hours" placeholder="hrs" onchange="setPlanHours(${p.row}, this.value)"
        onclick="event.stopPropagation()">
      <span class="plan-hours-label">h</span>
    </div>
  </div>`;
}

function upcomingBacklogHtml() {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const week  = new Date(today); week.setDate(today.getDate() + 7);
  const month = new Date(today); month.setDate(today.getDate() + 30);
  const groups = { overdue: [], today: [], week: [], month: [], later: [], none: [] };
  Object.entries(allTasks).forEach(([sheet, tasks]) => tasks.forEach(t => {
    if (t.status === 'Completed') return;
    const item = { ...t, sheet };
    const d = parseDeadline(t.deadline);
    if (!d) { groups.none.push(item); return; }
    const dt = new Date(d); dt.setHours(0, 0, 0, 0);
    if (dt < today)                            groups.overdue.push(item);
    else if (dt.getTime() === today.getTime()) groups.today.push(item);
    else if (dt <= week)                       groups.week.push(item);
    else if (dt <= month)                      groups.month.push(item);
    else                                       groups.later.push(item);
  }));
  const byDate = (a, b) => (parseDeadline(a.deadline) || 0) - (parseDeadline(b.deadline) || 0);
  const sections = [
    { key: 'overdue', label: 'Overdue',    color: 'var(--overdue-color)' },
    { key: 'today',   label: 'Today',      color: 'var(--today-color)'  },
    { key: 'week',    label: 'This Week',  color: 'var(--week-color)'   },
    { key: 'month',   label: 'This Month', color: 'var(--month-color)'  },
    { key: 'later',   label: 'Later',      color: 'var(--later-color)'  },
    { key: 'none',    label: 'No Deadline',color: 'var(--later-color)'  },
  ];
  const inner = sections.filter(s => groups[s.key].length).map(s => {
    const items = s.key === 'none' ? groups[s.key] : groups[s.key].sort(byDate);
    return `<div class="section">
      <div class="section-title" style="color:${s.color}">${s.label} &mdash; ${items.length} task${items.length !== 1 ? 's' : ''}</div>
      <table class="task-table"><tbody>${items.map(t => upcomingRow(t)).join('')}</tbody></table>
    </div>`;
  }).join('');
  return `<div class="section-title" style="margin:6px 0 10px">📋 All tasks &mdash; drag onto a day above, or use 📌 to plan for today</div>`
       + (inner || '<div class="empty">No upcoming tasks.</div>');
}

function upcomingRow(t) {
  const pillClass = deadlinePillClass(parseDeadline(t.deadline));
  const deadlineHtml = t.deadline
    ? `<span class="deadline-pill ${pillClass}">${escHtml(t.deadline)}</span>`
    : '';
  const meta = [t.sheet, t.hours ? `⏱ ${t.hours}h` : ''].filter(Boolean).join('  ·  ');
  return `<tr class="task-row" draggable="true" data-sheet="${escHtml(t.sheet)}" data-row="${t.row}"
             ondragstart="onBacklogDragStart(event)" ondragend="event.currentTarget.classList.remove('dragging-task')">
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
      <button class="icon-btn" title="Add to today's plan" onclick="addPlanToDay('${jsStr(t.sheet)}',${t.row},todayIso())">📌</button>
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
  if (guardDemo()) return;
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
  document.getElementById('today-picker').classList.remove('open');
});

// ── Weekly planner ─────────────────────────────────────────────────────────
// Plan entries live server-side in the _plan sheet (via /api/plan), keyed by an
// explicit ISO day — so a planned task never vanishes at midnight; it just sits
// on its date. `order` is its priority within the day; `hours` is an estimate.

function isoDay(d) {
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function todayIso() { return isoDay(new Date()); }
function startOfToday() { const d = new Date(); d.setHours(0, 0, 0, 0); return d; }
function weekDates(offset) {
  // Rolling 7-day window starting today (offset shifts it by whole weeks).
  const base = startOfToday(); base.setDate(base.getDate() + offset * 7);
  return Array.from({ length: 7 }, (_, i) => { const x = new Date(base); x.setDate(base.getDate() + i); return x; });
}
function changeWeek(delta) { _weekOffset += delta; renderContent(); }
function gotoThisWeek() { _weekOffset = 0; renderContent(); }

function taskFor(project, taskRow) {
  return (allTasks[project] || []).find(t => t.row === taskRow) || null;
}
function planEntry(project, taskRow, day) {
  return allPlan.find(p => p.project === project && p.task_row === taskRow && p.day === day);
}

async function addPlanToDay(project, taskRow, day) {
  if (guardDemo()) return;
  if (planEntry(project, Number(taskRow), day)) { renderContent(); return; }  // already planned that day
  const dayEntries = allPlan.filter(p => p.day === day);
  const order = dayEntries.length ? Math.max(...dayEntries.map(p => p.order)) + 1 : 0;
  const t = taskFor(project, Number(taskRow));
  const hours = (t && t.hours) ? t.hours : '';   // prefill from the task's estimate, else empty
  await fetch('/api/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project, task_row: Number(taskRow), day, order, hours }) });
  await loadPlan(); refreshSyncStatus(); renderContent();
}

async function removePlan(row) {
  if (guardDemo()) return;
  await fetch(`/api/plan/${row}`, { method: 'DELETE' });
  await loadPlan(); refreshSyncStatus(); renderContent();
}

async function setPlanHours(row, val) {
  if (guardDemo()) return;
  await fetch(`/api/plan/${row}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hours: val }) });
  await loadPlan(); refreshSyncStatus(); renderContent();
}

async function movePlanToDay(row, day) {
  if (guardDemo()) return;
  const p = allPlan.find(x => x.row === row); if (!p || p.day === day) return;
  const dayEntries = allPlan.filter(x => x.day === day);
  const order = dayEntries.length ? Math.max(...dayEntries.map(x => x.order)) + 1 : 0;
  await fetch(`/api/plan/${row}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ day, order }) });
  await loadPlan(); refreshSyncStatus(); renderContent();
}

async function movePlanRank(row, dir) {
  if (guardDemo()) return;
  const p = allPlan.find(x => x.row === row); if (!p) return;
  const entries = allPlan.filter(x => x.day === p.day).sort((a, b) => (a.order - b.order) || (a.row - b.row));
  const idx = entries.findIndex(x => x.row === row);
  const swap = idx + dir;
  if (swap < 0 || swap >= entries.length) return;
  entries.splice(swap, 0, entries.splice(idx, 1)[0]);   // reorder locally
  // Persist any entry whose position (=new order) changed.
  const puts = [];
  entries.forEach((e, i) => {
    if (e.order !== i) puts.push(fetch(`/api/plan/${e.row}`, { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order: i }) }));
  });
  if (puts.length) await Promise.all(puts);
  await loadPlan(); refreshSyncStatus(); renderContent();
}

// ── Planner drag & drop ──
function onBacklogDragStart(e) {
  e.dataTransfer.effectAllowed = 'copy';
  e.dataTransfer.setData('text/plain', JSON.stringify({ type: 'task',
    sheet: e.currentTarget.dataset.sheet, row: Number(e.currentTarget.dataset.row) }));
  e.currentTarget.classList.add('dragging-task');
}
function onPlanCardDragStart(e, row) {
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', JSON.stringify({ type: 'plan', row }));
  e.currentTarget.classList.add('dragging-task');
}
function onDayDragOver(e) {
  e.preventDefault(); e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
async function onDayDrop(e, day) {
  e.preventDefault(); e.currentTarget.classList.remove('drag-over');
  let data; try { data = JSON.parse(e.dataTransfer.getData('text/plain')); } catch (_) { return; }
  if (!data) return;
  if (data.type === 'task') await addPlanToDay(data.sheet, data.row, day);
  else if (data.type === 'plan') await movePlanToDay(data.row, day);
}

// ── Add-to-day picker (reuses #today-picker) ──
function openDayPicker(e, day) {
  e.stopPropagation();
  _planPickerDay = day;
  const picker = document.getElementById('today-picker');
  picker.classList.add('open');
  const d = new Date(day + 'T00:00:00');
  document.getElementById('today-picker-head').textContent =
    'Add to ' + d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
  document.getElementById('today-picker-search').value = '';
  renderTodayPickerList();
  const ph = picker.offsetHeight, pw = picker.offsetWidth;
  const top = (e.clientY + 8 + ph > window.innerHeight - 8) ? e.clientY - ph - 8 : e.clientY + 8;
  picker.style.top  = Math.max(8, top) + 'px';
  picker.style.left = Math.min(e.clientX, window.innerWidth - pw - 8) + 'px';
  setTimeout(() => document.getElementById('today-picker-search').focus(), 0);
}

function closeTodayPicker() {
  document.getElementById('today-picker').classList.remove('open');
}

function renderTodayPickerList() {
  const q = document.getElementById('today-picker-search').value.trim().toLowerCase();
  const day = _planPickerDay;
  const planned = new Set(allPlan.filter(p => p.day === day).map(p => p.project + '::' + p.task_row));
  const list = document.getElementById('today-picker-list');
  const items = [];
  Object.entries(allTasks).forEach(([sheet, tasks]) => {
    tasks.forEach(t => {
      if (t.status === 'Completed') return;
      if (planned.has(sheet + '::' + t.row)) return;
      if (q && !t.task.toLowerCase().includes(q) && !sheet.toLowerCase().includes(q)) return;
      items.push({ ...t, sheet });
    });
  });
  if (!items.length) {
    list.innerHTML = '<div class="today-picker-empty">No matching tasks.</div>';
    return;
  }
  list.innerHTML = items.slice(0, 40).map(t => `
    <div class="today-picker-item" onclick="addPlanToDay('${jsStr(t.sheet)}',${t.row},'${jsStr(day)}');closeTodayPicker();">
      <div>${escHtml(t.task)}</div>
      <div class="tpi-sheet">${escHtml(t.sheet)}${t.deadline ? ' &middot; ' + escHtml(t.deadline) : ''}</div>
    </div>
  `).join('');
}

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
  if (guardDemo()) { document.getElementById('task-modal').classList.remove('open'); return; }
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
  if (guardDemo()) return;
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
    const p = parseNote(n.note);
    const bodyStyle = `font-family:${p.font||'inherit'};font-size:${p.size||14}px`;
    const titleHtml = p.title ? `<div class="note-title">${escHtml(p.title)}</div>` : '';
    return `<div class="note-card" style="border-left-color:${escHtml(borderColor)}">
      <div class="note-header">
        <span class="note-project">${escHtml(n.project)}</span>
        <span class="note-badge imp-${escHtml(n.importance)}">${escHtml(n.importance)}</span>
        <span class="note-badge pur-${escHtml(n.purpose)}">${escHtml(n.purpose)}</span>
      </div>
      ${titleHtml}
      <div class="note-body" style="${bodyStyle}">${sanitizeNoteHTML(p.body)}</div>
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

function parseNote(raw) {
  try {
    const p = JSON.parse(raw);
    if (p && p.v === 2) return p;
  } catch {}
  // Legacy plain text — escape and preserve newlines
  return { v:1, title:'', font:'', size:'14', body: escHtml(raw).replace(/\n/g, '<br>') };
}

function sanitizeNoteHTML(html) {
  const d = document.createElement('div');
  d.innerHTML = html;
  d.querySelectorAll('script,iframe,object,embed,style,link').forEach(el => el.remove());
  d.querySelectorAll('*').forEach(el => {
    [...el.attributes].forEach(a => { if (a.name.startsWith('on')) el.removeAttribute(a.name); });
  });
  return d.innerHTML;
}

function execFmt(cmd) {
  document.execCommand('styleWithCSS', false, false);
  document.execCommand(cmd, false, null);
  document.getElementById('n-editor').focus();
}

function applyNoteFont() {
  const ed = document.getElementById('n-editor');
  ed.style.fontFamily = document.getElementById('n-font').value || '';
  ed.style.fontSize   = (document.getElementById('n-size').value || '14') + 'px';
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

  const ed = document.getElementById('n-editor');
  if (row) {
    const n = allNotes.find(x => x.row === row);
    if (n) {
      const p = parseNote(n.note);
      npSel.value = n.project;
      document.getElementById('n-title').value = p.title || '';
      ed.innerHTML = sanitizeNoteHTML(p.body || '');
      document.getElementById('n-font').value = p.font || '';
      document.getElementById('n-size').value = p.size || '14';
      ed.style.fontFamily = p.font || '';
      ed.style.fontSize   = (p.size || '14') + 'px';
      document.getElementById('n-importance').value = n.importance;
      document.getElementById('n-purpose').value = n.purpose;
      selectedNoteColor = n.color || NOTE_COLORS[0];
    }
  } else {
    if (activeSheet) npSel.value = activeSheet;
    document.getElementById('n-title').value = '';
    ed.innerHTML = '';
    ed.style.fontFamily = '';
    ed.style.fontSize = '14px';
    document.getElementById('n-font').value = '';
    document.getElementById('n-size').value = '14';
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
  if (guardDemo()) { document.getElementById('note-modal').classList.remove('open'); return; }
  const project    = document.getElementById('n-project').value;
  const title      = document.getElementById('n-title').value.trim();
  const font       = document.getElementById('n-font').value;
  const size       = document.getElementById('n-size').value;
  const rawBody    = document.getElementById('n-editor').innerHTML.trim();
  const textContent= document.getElementById('n-editor').textContent.trim();
  const importance = document.getElementById('n-importance').value;
  const purpose    = document.getElementById('n-purpose').value;
  if (!textContent && !title) { alert('Please add a title or note text.'); return; }
  const note = JSON.stringify({ v:2, title, font, size, body: sanitizeNoteHTML(rawBody) });
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
  if (guardDemo()) return;
  if (!confirm('Delete this note?')) return;
  await fetch(`/api/notes/${row}`, { method: 'DELETE' });
  await loadNotes();
  renderContent();
}

// ── Literature view ───────────────────────────────────────────────────────

function renderLiterature() {
  const content = document.getElementById('content');
  // Only (re)build the toolbar on first entry into this view, so the search
  // input's DOM node — and therefore focus/caret — survives re-renders
  // triggered by its own oninput.
  if (!document.getElementById('lit-toolbar')) {
    const projectOpts = ['', ...allSheets.map(s => s.name)]
      .map(p => `<option value="${escHtml(p)}">${p || 'All Projects'}</option>`).join('');
    content.innerHTML = `
      <div class="notes-toolbar" id="lit-toolbar">
        <select id="l-filter-project" onchange="renderLiteratureCards()">${projectOpts}</select>
        <input id="l-search" type="search" class="lit-search-input" placeholder="🔍 Search titles…" oninput="renderLiteratureCards()">
        <button id="lit-export-btn" onclick="exportBib()"
                style="padding:7px 12px;background:none;border:1.5px solid var(--border);border-radius:8px;
                       font-size:12px;cursor:pointer;color:var(--text-muted);white-space:nowrap;margin-left:auto">
          ⬇ Export .bib
        </button>
      </div>
      <div id="lit-cards-wrap"></div>
    `;
  }
  renderLiteratureCards();
}

function renderLiteratureCards() {
  const wrap = document.getElementById('lit-cards-wrap');
  if (!wrap) return;
  const projectFilter = document.getElementById('l-filter-project')?.value || '';
  const searchQ = (document.getElementById('l-search')?.value || '').trim().toLowerCase();
  let items = allLiterature;
  if (projectFilter) items = items.filter(l => l.project === projectFilter);
  if (searchQ) items = items.filter(l => (l.title || '').toLowerCase().includes(searchQ));

  if (!items.length) {
    wrap.innerHTML = `<div class="empty">${allLiterature.length ? 'No matching references.' : 'No literature yet. Click "+ Add Reference" to create one.'}</div>`;
    return;
  }

  const byProject = {};
  items.forEach(l => { (byProject[l.project] = byProject[l.project] || []).push(l); });

  const projectNames = allSheets.map(s => s.name).filter(p => byProject[p]);
  Object.keys(byProject).forEach(p => { if (!projectNames.includes(p)) projectNames.push(p); });

  const byModified = (a, b) => (b.modified || '').localeCompare(a.modified || '') || (b.row - a.row);
  const cards = projectNames.map(project => {
    const refs = byProject[project].slice().sort(byModified);
    const fd = FLOWER_DEFS[hashStr(project) % FLOWER_DEFS.length];
    const bloom = refs.map(() => ({ status: 'Completed' }));  // every petal blooms — one per reference
    const rows = refs.map(l => litRow(l)).join('');
    return `<div class="lit-project-card" style="border-color:${fd.stroke}">
      <div class="lit-card-header" style="background:${fd.empty}">
        <div class="lit-card-flower">${flowerSVG(project, bloom, 32)}</div>
        <div class="lit-card-heading">
          <div class="lit-card-title" style="color:${fd.center}">${escHtml(project)}</div>
          <div class="lit-card-count" style="color:${fd.stroke}">${refs.length} reference${refs.length !== 1 ? 's' : ''}</div>
        </div>
      </div>
      <div class="lit-card-body"><table class="task-table"><tbody>${rows}</tbody></table></div>
    </div>`;
  }).join('');

  wrap.innerHTML = `<div class="lit-cards">${cards}</div>`;
}

function litRow(l) {
  const titleHtml = l.link
    ? `<a href="${escHtml(l.link)}" target="_blank" rel="noopener noreferrer">${escHtml(l.title)}</a>`
    : escHtml(l.title);
  const metaParts = [l.authors, l.year].filter(Boolean);
  return `<tr class="task-row">
    <td style="width:100%">
      <div class="lit-title">${titleHtml}</div>
      ${metaParts.length ? `<div class="task-meta">${escHtml(metaParts.join('  ·  '))}</div>` : ''}
      <div class="lit-note-panel" id="lit-note-${l.row}" hidden>${l.notes ? escHtml(l.notes) : 'No notes yet.'}</div>
    </td>
    <td style="white-space:nowrap">
      <button class="icon-btn" title="Show notes" onclick="toggleLitNote(${l.row})">🗒</button>
      <button class="icon-btn" title="Edit" onclick="openLitModal(${l.row})">✏️</button>
      <button class="icon-btn" title="Delete" onclick="deleteLit(${l.row})">🗑</button>
    </td>
  </tr>`;
}

function toggleLitNote(row) {
  const el = document.getElementById(`lit-note-${row}`);
  if (el) el.hidden = !el.hidden;
}

function openLitModal(row = null) {
  if (typeof row !== 'number') row = null;  // guard against click-event being passed
  litEditRow = row;
  document.getElementById('lit-modal-title').textContent = row ? 'Edit Reference' : 'Add Reference';

  const lpSel = document.getElementById('l-project');
  lpSel.innerHTML = allSheets.map(s => `<option>${escHtml(s.name)}</option>`).join('');

  if (row) {
    const l = allLiterature.find(x => x.row === row);
    if (l) {
      lpSel.value = l.project;
      document.getElementById('l-title').value   = l.title   || '';
      document.getElementById('l-link').value    = l.link    || '';
      document.getElementById('l-authors').value = l.authors || '';
      document.getElementById('l-year').value    = l.year    || '';
      document.getElementById('l-notes').value   = l.notes   || '';
    }
  } else {
    if (activeSheet) lpSel.value = activeSheet;
    document.getElementById('l-title').value   = '';
    document.getElementById('l-link').value    = '';
    document.getElementById('l-authors').value = '';
    document.getElementById('l-year').value    = '';
    document.getElementById('l-notes').value   = '';
  }
  document.getElementById('lit-modal').classList.add('open');
}

function closeLitModal() {
  document.getElementById('lit-modal').classList.remove('open');
}

async function saveLit() {
  if (guardDemo()) { closeLitModal(); return; }
  const project = document.getElementById('l-project').value;
  const title   = document.getElementById('l-title').value.trim();
  const link    = document.getElementById('l-link').value.trim();
  const authors = document.getElementById('l-authors').value.trim();
  const year    = document.getElementById('l-year').value.trim();
  const notes   = document.getElementById('l-notes').value.trim();
  if (!title) { alert('Please add a title.'); return; }
  const body = { project, title, link, authors, year, notes };
  const url    = litEditRow ? `/api/literature/${litEditRow}` : '/api/literature';
  const method = litEditRow ? 'PUT' : 'POST';
  const res = await fetch(url, {
    method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  if (!res.ok) { alert('Failed to save reference. Check the server log.'); return; }
  closeLitModal();
  await loadLiterature();
  renderContent();
}

async function deleteLit(row) {
  if (guardDemo()) return;
  if (!confirm('Delete this reference?')) return;
  await fetch(`/api/literature/${row}`, { method: 'DELETE' });
  await loadLiterature();
  renderContent();
}

function bibEscape(s) {
  return String(s || '').replace(/[{}]/g, '').replace(/[\r\n]+/g, ' ');
}

function bibKey(l) {
  let base = '';
  if (l.authors) base = l.authors.split(',')[0].trim().split(/\s+/).pop().replace(/[^A-Za-z]/g, '');
  if (!base) base = (l.title || 'ref').split(/\s+/)[0].replace(/[^A-Za-z]/g, '');
  return `${base || 'ref'}${l.year || ''}_${l.row}`;
}

function toBibEntry(l) {
  const fields = [`  title = {${bibEscape(l.title)}}`];
  if (l.authors) fields.push(`  author = {${bibEscape(l.authors)}}`);
  if (l.year)    fields.push(`  year = {${bibEscape(l.year)}}`);
  if (l.link)    fields.push(`  url = {${bibEscape(l.link)}}`);
  if (l.notes)   fields.push(`  note = {${bibEscape(l.notes)}}`);
  return `@misc{${bibKey(l)},\n${fields.join(',\n')}\n}`;
}

function exportBib() {
  const projectFilter = document.getElementById('l-filter-project')?.value || '';
  const items = projectFilter ? allLiterature.filter(l => l.project === projectFilter) : allLiterature;
  if (!items.length) { alert('No literature entries to export.'); return; }
  const bib = items.map(toBibEntry).join('\n\n');
  const a = document.createElement('a');
  a.href = 'data:application/x-bibtex;charset=utf-8,' + encodeURIComponent(bib);
  a.download = (projectFilter || 'all-literature') + '.bib';
  a.click();
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
  if (guardDemo()) { document.getElementById('collab-modal').classList.remove('open'); return; }
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
  if (guardDemo()) return;
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
  if (guardDemo()) { document.getElementById('project-modal').classList.remove('open'); return; }
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
  if (guardDemo()) return;
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

// ── GarDone ───────────────────────────────────────────────────────────────

function renderGarDone() {
  const projects = Object.keys(allTasks).sort();
  let totalDone = 0, totalHours = 0;
  const specimenCards = [];

  for (const project of projects) {
    const tasks   = allTasks[project] || [];
    const done    = tasks.filter(t => t.status === 'Completed');
    if (!done.length) continue;
    totalDone += done.length;
    const hours = done.reduce((s, t) => s + (parseFloat(t.hours) || 0), 0);
    totalHours += hours;

    const fd      = FLOWER_DEFS[hashStr(project) % FLOWER_DEFS.length];
    const accent  = fd.stroke;
    const fill    = fd.fill;
    const flower  = flowerSVG(project, done, 130);

    const taskRows = done.map(t => {
      const date = t.completed_date
        ? `<span class="gardone-task-date">${escHtml(t.completed_date)}</span>` : '';
      return `<li class="gardone-task-entry">
        <span class="gardone-tick" style="color:${accent}">✓</span>
        <span class="gardone-task-name">${escHtml(t.task)}</span>
        ${date}
      </li>`;
    }).join('');

    const statsStr = `${done.length} of ${tasks.length} done${hours ? '  ·  ' + hours.toFixed(0) + 'h' : ''}`;

    specimenCards.push(`
      <div class="gardone-specimen">
        <div class="gardone-inner">
          <div class="gardone-flower-col">
            <div class="gardone-flower-ring" style="border-color:${fill}">${flower}</div>
          </div>
          <div class="gardone-notes-col">
            <div class="gardone-spec-header" style="border-bottom-color:${accent}">
              <span class="gardone-spec-name">${escHtml(project)}</span>
              <span class="gardone-spec-stats">${escHtml(statsStr)}</span>
            </div>
            <ul class="gardone-task-list">${taskRows}</ul>
          </div>
        </div>
        <div class="gardone-stamp" style="color:${accent};border-color:${accent}">DONE</div>
      </div>`);
  }

  const hoursLine = totalHours
    ? `<span class="gardone-pill-sep">·</span><span>${totalHours.toFixed(0)}h logged</span>` : '';
  const today = new Date().toLocaleDateString('en-GB', {day:'numeric', month:'long', year:'numeric'});

  document.getElementById('content').innerHTML = `
    <div class="gardone-wrap">
      <div class="gardone-header">
        <div class="gardone-title">🌸 GarDone</div>
        <div class="gardone-subtitle">a garden of things done</div>
        <div class="gardone-stats-pill">
          <span>${totalDone} tasks</span>
          ${hoursLine}
          <span class="gardone-pill-sep">·</span>
          <span>${specimenCards.length} projects</span>
        </div>
        <div style="font-size:12px;color:#A08060;margin-top:12px;font-style:italic">${today}</div>
      </div>
      ${specimenCards.length
        ? `<div class="gardone-grid">${specimenCards.join('')}</div>`
        : `<div class="gardone-empty">No completed tasks yet — finish something first 🌱</div>`}
    </div>`;
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
  if (guardDemo()) return;
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
  if (guardDemo()) return;
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
