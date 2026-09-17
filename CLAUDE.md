# CLAUDE.md — Research Task Manager

This file gives AI coding agents the context needed to work effectively in this codebase.

---

## What this project is

A single-file Flask web app that serves as a task manager for researchers. Data is stored in Google Sheets (one tab per project) or in a local JSON file when no Sheets credentials are configured. The entire backend + frontend ships as one Python file: `app.py`.

---

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py          # opens http://localhost:8080 automatically
```

On macOS, users can also double-click `start.command`. On Windows, `start.bat`.

---

## Architecture

### Single-file design
Everything lives in `app.py`:
- Python imports and constants (top)
- In-memory cache layer and local-mode shim
- Flask API routes (`/api/...`)
- The entire HTML/CSS/JS frontend as a Python raw string: `HTML = r"""..."""`
- `@app.route("/")` returns this string directly — no templates, no static files

### Two operating modes
`REMOTE_ENABLED = bool(SHEET_ID) and _GSPREAD_AVAILABLE`; `LOCAL_MODE = not REMOTE_ENABLED`. Set at startup, never changes at runtime.

| Mode | Storage | Triggered by |
|------|---------|--------------|
| **Dual** (local + Sheets, offline-capable) | `~/.research-tasks/local_data.json` mirror **is the read/source of truth**; every edit is also mirrored to Google Sheets — live when online, or journalled and replayed on reconnect | `config.py` with a valid `SHEET_ID` (+ gspread importable) |
| Local only | the same mirror file | No `config.py`, or `SHEET_ID` empty, or gspread missing |

Reads **never hit the network** in either mode — they come from the in-memory caches, which are loaded from the mirror on first use. First launch with an empty (or pre-dual, `schema < 2`) mirror does a one-time read-only **seed pull** from Sheets, then trusts the mirror.

### Offline journal & sync (dual mode)
The whole sheet is **never rewritten** — there is no blind overwrite. Each edit makes the same surgical per-row call it always did:
- `_enqueue_or_apply(op)` — if the pending journal is empty and online, apply the op to Sheets now; otherwise append it to `_pending_ops` (persisted in the mirror) and mark offline.
- `_drain_pending()` — replay the journal to Sheets **in order**, stopping at the first failure (keeps the rest queued). Runs on: first load, a background daemon thread (`_sync_loop`, every `SYNC_INTERVAL`s, only while something is pending), and `POST /api/sync`.
- `_apply_remote_op` / `_remote_ws` — perform one op; the remote tab is auto-created (with headers) on first write.
- Mirror writes are atomic (`temp + os.replace`) so a crash mid-write can't corrupt it.
- `sync_status()` → `{remote_enabled, local_mode, online, pending, last_synced}`; the frontend polls `/api/status` and shows a **Synced / Offline (N pending) / Syncing** pill.

### `_LocalWS` / `_DualWS` — the storage shims
All mutation routes call `ws.update(...)`, `ws.append_row(...)`, `ws.delete_rows(...)`. `_LocalWS` backs those with the in-memory caches + `_local_save()`. `_DualWS(_LocalWS)` does the same **and** mirrors the op to Sheets via `_enqueue_or_apply` (live or journalled). `get_worksheet`/`ensure_meta_ws` return a `_DualWS` when `REMOTE_ENABLED` else a `_LocalWS` (via `_make_ws`). **All API routes work unchanged across modes** — never add mode branches inside a route. (`append_meta_cache`/`delete_meta_cache_row` skip when `isinstance(ws, _LocalWS)` — true for `_DualWS` too — because those ops are not idempotent and the shim already applied them to the cache.)

### In-memory cache
```
_data_cache   = {}   # {project_name: [rows]}
_notes_cache  = []   # rows from _notes sheet
_collabs_cache= []   # rows from _collabs sheet
_lit_cache    = []   # rows from _literature sheet
_plan_cache   = []   # rows from _plan sheet
_pending_ops  = []   # journalled Sheets ops awaiting replay (dual mode, persisted in the mirror)
_ws_cache     = {}   # {title: _LocalWS/_DualWS}
DATA_CACHE_TTL = float('inf')  # never auto-expire
```

**Reads come from the caches (loaded once from the mirror); writes patch the cache in place and never trigger a full re-fetch.** Every mutation:
- Tasks: `ws.update(...)` then `patch_cache(sheet, row, values)` — updates one row in `_data_cache`. Idempotent, so safe even though the shim's `update()` already patched the same cache.
- Notes/collabs/literature/plan: `ws.update(...)` then `patch_meta_cache(cache_list, row, values)` (same idempotent pattern). For `ws.append_row(...)` / `ws.delete_rows(...)`, use `append_meta_cache(cache_list, ws, values)` / `delete_meta_cache_row(cache_list, ws, row)` — **not** idempotent, and the shim already applied them to the cache, so they no-op when `isinstance(ws, _LocalWS)` (true for `_DualWS`) and only run for a raw gspread `ws`.
- `_local_save()` persists all caches + `_pending_ops` after each mutation (atomically).
- `invalidate_cache()` / `_reset_caches()` — zero the caches; `invalidate_cache` also resets the load timestamp. **Do not call from a mutation route.** `POST /api/sync` no longer re-pulls; it drains the journal. A full seed pull happens only via `_try_pull_from_remote()` on an empty/pre-dual mirror.

### Row numbering
Sheets rows are **1-indexed**. Row 1 is always the header row. `parse_tasks()` skips `rows[1:]`, so tasks start at row 2. When creating a new project, always initialise `_data_cache[name] = [TASK_HEADERS]` (not `[]`) so the first added task lands at row 2, not row 1.

---

## API routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sheets` | List projects with active task count |
| GET | `/api/tasks?sheet=X` | All tasks (or filtered to one sheet) |
| POST | `/api/tasks` | Add task — body: `{sheet, task, deadline, hours, status, assignee}` |
| PUT | `/api/tasks/<sheet>/<row>` | Update task fields |
| DELETE | `/api/tasks/<sheet>/<row>` | Clear a task row |
| GET | `/api/notes?project=X` | All notes (or filtered) |
| POST | `/api/notes` | Add note |
| PUT | `/api/notes/<row>` | Edit note |
| DELETE | `/api/notes/<row>` | Delete note |
| GET | `/api/collaborators?project=X` | All collaborators |
| POST | `/api/collaborators` | Add collaborator |
| DELETE | `/api/collaborators/<row>` | Remove collaborator |
| GET | `/api/literature?project=X` | All literature references (or filtered) |
| POST | `/api/literature` | Add reference — body: `{project, title, link, authors, year, notes}` |
| PUT | `/api/literature/<row>` | Edit reference |
| DELETE | `/api/literature/<row>` | Delete reference |
| GET | `/api/plan` | All weekly-plan entries |
| POST | `/api/plan` | Plan a task — body: `{project, task_row, day (ISO), order, hours}` |
| PUT | `/api/plan/<row>` | Edit a plan entry (day / order / hours) |
| DELETE | `/api/plan/<row>` | Remove a plan entry |
| POST | `/api/projects` | Create project (sheet tab) |
| DELETE | `/api/projects/<name>` | Delete project |
| POST | `/api/sync` | Drain the pending offline journal to Sheets (never overwrites local); returns sync status |
| GET | `/api/status` | Returns `{remote_enabled, local_mode, online, pending, last_synced}` |

All mutation routes use `request.get_json(silent=True) or {}` — never `request.json`.

---

## Google Sheet structure

Each project tab: columns A–F

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| Deadline | Task | Hours | Status | Completed Date | Assignee |

Four hidden meta-tabs (never delete or rename; all in `META_SHEETS`):
- `_notes` — columns: Project, Note, Importance, Purpose, Color, Created, Modified
- `_collabs` — columns: Project, Name, Role
- `_literature` — columns: Project, Title, Link, Authors, Year, Notes, Created, Modified, Read (`"1"`/empty; appended last so legacy 8-col rows still parse — `read` defaults False)
- `_plan` — columns: Project, TaskRow, Day (ISO `YYYY-MM-DD`), Order, Hours, Created, Modified

---

## Frontend (JavaScript inside the HTML string)

### Key globals
```javascript
allSheets  = []   // from /api/sheets
allTasks   = {}   // {projectName: [taskObjects]}
allNotes   = []   // flat array of note objects
allCollabs = []   // flat array of collab objects
allLiterature = [] // flat array of literature reference objects
activeSheet  = null   // null = "All Projects"
viewMode     = 'tasks' | 'upcoming' | 'notes' | 'literature' | 'collaborators' | 'stats' | 'procrastinate' | 'gardone'
demoMode     = bool  // true = use DEMO_DATA instead of API
_projectOrder = string[] | null  // custom sidebar order from localStorage
```

### Rendering flow
```
renderContent()          ← always calls updateTopbar() first
  ├── renderTasks()      — task list + garden view
  ├── renderUpcoming()   — weekly planner board (rolling 7 days from today) + deadline-grouped backlog
  ├── renderNotes()      — note cards
  ├── renderLiterature() / renderLiteratureCards() — two-column per-project cards, scrollable, searchable, .bib export
  ├── renderCollaborators()
  ├── renderStats()
  ├── renderGarDone()    — botanical specimen view of completed tasks
  └── renderProcrastinate() — snake game + quotes
```

Navigation (`selectSheet`, `selectUpcoming`, `selectView`) always calls `renderContent()` directly using in-memory data — **never triggers a network fetch**.

`renderContent()` calls `updateTopbar()` at the top — this is the single place that syncs topbar state (filter row visibility, add/search/export button state) with `viewMode`. Do not call `updateTopbar()` separately from navigation functions.

### XSS rules — critical
Two escaping helpers exist for different contexts:

| Helper | Use for | Why |
|--------|---------|-----|
| `escHtml(s)` | HTML text content and HTML attribute values | Encodes `<`, `>`, `&`, `"`, `'` as HTML entities |
| `jsStr(s)` | String values inside JS string literals in `onclick="fn('...')"` attributes | Uses backslash-escaping; HTML entities in onclick are decoded by the HTML parser *before* JS runs, breaking out of the string |

**Rule:** anything user-controlled that goes inside `onclick="fn('VALUE')"` must use `jsStr(VALUE)`, not `escHtml(VALUE)`.

```javascript
// CORRECT
`<button onclick="deleteProject('${jsStr(name)}')">…</button>`
`<div class="name">${escHtml(name)}</div>`

// WRONG — &#39; in onclick decodes to ' before JS runs = XSS
`<button onclick="deleteProject('${escHtml(name)}')">…</button>`
```

### SVG flower engine
`flowerSVG(projectName, tasks, size)` — deterministic flower per project, one petal per task, petal fills on completion. Flower type is `hashStr(projectName) % 6`. Defined in `FLOWER_DEFS`. Do not change petal sizing math without testing at both N=1 and N=30.

### Theme system
Five CSS variable sets in `THEMES` object. `applyTheme(name)` writes all `--` variables to `:root` and saves to `localStorage`. Always use `var(--accent)` etc. in CSS, never hardcode colours.

---

## Adding a new feature — checklist

1. **New API route** → add Flask route, use `get_worksheet()` for Sheets ops, call `patch_cache()` or `invalidate_cache()` after writes, handle local mode via `_LocalWS` (not inline `if LOCAL_MODE`)
2. **New view** → add `viewMode` value, add a branch in `renderContent()`, add a sidebar button with matching `onclick="selectView('yourview')"`
3. **New field on tasks** → extend `TASK_HEADERS`, update `parse_tasks()`, update `api_add_task` and `api_update_task`, update the modal HTML and `saveModal()`/`openEditModal()`
4. **User-controlled string in onclick** → use `jsStr()`, not `escHtml()`

---

## Files

| File | Purpose |
|------|---------|
| `app.py` | Entire app — backend + frontend |
| `config.py` | `SHEET_ID` and `CREDS_FILE` — **gitignored**, never commit |
| `config.example.py` | Template for `config.py` |
| `service_account.json` | Google service account key — **gitignored**, never commit |
| `local_data.json` | Local-mode data store — **gitignored** |
| `requirements.txt` | `flask`, `gspread`, `google-auth` |
| `start.command` | macOS double-click launcher |
| `start.bat` | Windows double-click launcher |
| `demo/` | Screenshots for README |

---

## Implemented features (complete list)

- Project management: create, delete, switch between projects
- Sidebar drag-to-reorder: drag projects to reorder, persisted in `localStorage.projectOrder`
- Tasks: add, edit, delete, inline status cycle, date picker deadline
- Bulk status change: checkbox-select multiple tasks, apply status to all
- Search: real-time filter across all projects
- Export CSV: current project or all projects
- Upcoming view: a **weekly planner board** (a rolling 7-day window starting **today**, horizontally scrollable, prev/next-week nav + a "Today" reset, today's column highlighted and labelled "Today"; each column scrolls vertically) over a **backlog** of all incomplete tasks grouped Overdue / Today / This Week / This Month / Later. Drag a backlog task onto a day to plan it, drag a plan card between days to move it, or use a day's `+` picker / a backlog row's 📌 (plan for today). Each plan card shows a **priority rank** with ▲▼ to reorder, a **status badge** (click → status picker; changing it updates the underlying task everywhere), and an **estimated-hours** input (prefilled from the task's own estimate on add). Per-day and week hour totals are summed in the headers. Plans are stored server-side in `_plan` by explicit date, so nothing disappears at midnight. **Auto-roll**: on load / entering the view, any plan entry whose day has passed and whose task isn't `Completed` is moved forward to today (`maybeRollPlans()`); completed entries stay put as a record, and entries whose task was deleted are left alone.
- Offline / dual sync: works with no internet off the local mirror; edits journal and replay to Google Sheets on reconnect. A topbar pill shows **Synced / Offline (N pending) / Syncing**; the **Sync** button pushes the journal.
- Stats view: overall %, hours logged, by-status breakdown, per-project bars
- Notes: rich text editor (bold/italic/underline/bullets/numbered lists), title, font family (5), font size; importance + purpose tags; color swatches; sorted newest-first
- Literature: each reference has a **read/unread toggle** (✅/⬜; read rows are dimmed), a **sort selector** (A–Z, Unread first, Read first — `#l-sort`), and within each project card entries are sorted by the chosen mode (default alphabetical by title, case-insensitive; the `.bib` export follows the same order). References grouped into a colored card per project (same visual language as GarDone/Garden — top border + tinted header use the project's `FLOWER_DEFS` accent, plus a small bloomed flower icon with one petal per reference), laid out two cards per row with each card's reference list independently scrollable (`.lit-card-body`, `max-height` + `overflow-y`); each row shows the title as a clickable link (URL itself hidden), a 🗒 button that toggles a hidden notes/annotation panel, edit and delete; add/edit via modal (title, link, authors, year, notes); project filter dropdown + title search box (`#l-search`) — the toolbar is only built once per view-entry (guarded by `if (!document.getElementById('lit-toolbar'))`) so retyping in the search box never rebuilds and steals focus from itself, it just re-renders `#lit-cards-wrap` via `renderLiteratureCards()`; client-side `.bib` export (per-project or all) for import into Zotero/reference managers
- Collaborators: add (comma-separated for bulk), delete, assignable to tasks
- Flower progress visualization: per-project SVG flower, one petal per task
- GarDone tab: botanical specimen view of all completed tasks, parchment aesthetic
- `wall_preview.py`: standalone script that generates GarDone as a static HTML file
- Five color themes (Classic, Ocean, Sage, Sunset, Lavender), persisted in localStorage
- Resizable sidebar, width persisted in localStorage
- Local mode: full offline operation with `local_data.json`
- Sync button: force re-fetch from Google Sheets
- Demo mode: toggle in sidebar footer loads hardcoded demo data (14 projects, 52 tasks); all mutations blocked; real data untouched
- Procrastinate tab: snake game with 1–5 min timer, deep quotes panel

## Intentionally deferred (do not implement without discussion)

- **Subtasks** — requires schema change (new sheet structure or encoding)
- **Recurring tasks** — requires schema change
- These were explicitly ruled out to keep the data model simple

## Data conventions

### Date format
Dates are stored and displayed as `"1 May 2026"` (human-readable).  
The `<input type="date">` uses ISO format `"2026-05-01"`.  
Two helpers handle the round-trip:
- `toDateInputValue(str)` — human string → ISO for the date input
- `fromDateInput(val)` — ISO → human string for storage

Always use these helpers; never store ISO dates directly.

### Status values
Exactly four, case-sensitive:
```
"Not Started" | "In Progress" | "Pending" | "Completed"
```
`ACTIVE_STATUSES = {"Not Started", "In Progress", "Pending"}` — used for sidebar counts.

### Note storage format
Notes are stored as a JSON string in the `Note` column of `_notes`:
```json
{"v":2,"title":"My Title","font":"Georgia, serif","size":"14","body":"<b>Bold</b> content…"}
```
`parseNote(raw)` handles the round-trip: tries `JSON.parse`; if it fails (legacy plain-text note), wraps the string as `{v:1, body: escHtml(raw)}`. Always use `parseNote()` when reading note content — never access `n.note` directly for display.

`sanitizeNoteHTML(html)` strips `<script>`, `<iframe>`, and `on*` attributes before rendering note body HTML.

### Note importance / purpose
Importance: `"High" | "Medium" | "Low"`  
Purpose: `"Design" | "Writing" | "Analysis" | "Planning" | "Other"`  
These map directly to CSS classes (`imp-High`, `pur-Design`, etc.) — adding new values requires adding CSS.

## Google Sheets specifics

### Rate limiting (429)
`_try_pull_from_remote()` (the one-time seed pull) retries up to 5 times with exponential backoff on 429. Because reads come from the mirror and writes are surgical per-row (not full-sheet rewrites), normal usage barely touches the API. Never add a read path that hits Sheets on every request — read from the caches.

### Sheet tab names
`_notes`, `_collabs`, `_literature`, and `_plan` are reserved meta-tabs. The `META_SHEETS` set keeps them out of the project list. Any new meta-tab must be added to `META_SHEETS`.

### gspread availability
`_GSPREAD_AVAILABLE` flag — gspread is optional. The app imports it in a try/except. In local mode gspread is never called. Do not call any gspread API outside of the `if not LOCAL_MODE` paths.

## localStorage keys

| Key | Value | Set by |
|-----|-------|--------|
| `theme` | `"classic"` \| `"ocean"` \| `"sage"` \| `"sunset"` \| `"lavender"` | `applyTheme()` |
| `sidebarWidth` | integer px | sidebar resize handler |
| `projectOrder` | JSON array of project names | sidebar drag-to-reorder |
| `demoMode` | `"1"` \| `"0"` | `toggleDemoMode()` |

The weekly plan is **no longer in localStorage** — it lives in the `_plan` sheet (via `/api/plan`), keyed by explicit ISO day, so a planned task never disappears at midnight and it syncs like everything else.

## CSS architecture

All colours go through CSS variables on `:root`. Never hardcode a colour in CSS.  
Adding a new theme means adding an entry to the `THEMES` JS object — the CSS already uses the variables.

Status badge classes are built by `statusCls(status)` = `'status-' + status.replace(/[^A-Za-z0-9]+/g,'-')`, so multi-word statuses become one hyphenated token: `.status-Not-Started`, `.status-In-Progress`, `.status-Pending`, `.status-Completed`. (Do **not** interpolate the raw status into the class — a space would split it into two classes and the pill styling would silently not apply.)  
Note badge classes: `imp-High`, `imp-Medium`, `imp-Low`, `pur-Design`, etc.

## Demo mode architecture

`DEMO_DATA` is a JS constant (baked into the HTML string) containing 14 projects, 52 tasks, 47 collaborators, 2 notes, 2 literature references, and an empty `plan` — matching the rough scale of real data.

`demoMode` is read from `localStorage` on startup. When true:
- `loadSheets/loadTasks/loadNotes/loadCollabs` return shallow copies of `DEMO_DATA` instead of hitting the API
- All mutation functions (`saveModal`, `deleteTask`, `pickStatus`, `bulkMark`, `bulkDelete`, `saveNote`, `deleteNote`, `saveLit`, `deleteLit`, `saveCollab`, `deleteCollab`, `saveNewProject`, `deleteProject`) call `guardDemo()` at the top and return early — nothing touches the real data
- `syncAll()` re-reads from `DEMO_DATA` instead of calling `/api/sync`

`toggleDemoMode(on)` resets `activeSheet` to null (project names differ between real and demo) but preserves `viewMode`.

## Topbar layout

The topbar uses `flex-wrap: wrap` with two logical rows:
- **Row 1**: `<h2>` title (flex:1) + `#topbar-right` div (sync, add buttons) — always visible
- **Row 2**: `#filter-area` (order:10, width:100%) — filter pills + search + export — only shown in task/upcoming views

`#filter-area` contains the filter buttons AND the search input and export button. `updateTopbar()` controls visibility of these elements and is called at the top of every `renderContent()` invocation.

## What NOT to do

- Do not read from Sheets inside a GET route that runs on every navigation — use the cache
- Do not add `if LOCAL_MODE` branches inside API routes — put the behaviour in `_LocalWS`
- Do not use `request.json` — use `request.get_json(silent=True) or {}`
- Do not use `render_template_string` — the route returns `HTML` directly
- Do not hardcode colours in CSS — use the CSS variable system
- Do not use `escHtml()` inside JS string literals in onclick attributes — use `jsStr()`
- Do not commit `config.py`, `service_account.json`, or `local_data.json`
