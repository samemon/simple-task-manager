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
`LOCAL_MODE = not bool(SHEET_ID)` — set at startup, never changes at runtime.

| Mode | Storage | Triggered by |
|------|---------|--------------|
| Google Sheets | gspread + service account | `config.py` present with a valid `SHEET_ID` |
| Local | `local_data.json` | No `config.py`, or `SHEET_ID` is empty |

### `_LocalWS` — the local-mode shim
All mutation routes call methods on a worksheet object (`ws.update(...)`, `ws.append_row(...)`, `ws.delete_rows(...)`). In local mode, `_LocalWS` provides the same interface backed by the in-memory caches + `_local_save()`. This means **all API routes work unchanged in both modes** — never add mode-specific branches inside a route; put them in `_LocalWS` instead.

### In-memory cache
```
_data_cache   = {}   # {project_name: [rows]}
_notes_cache  = []   # rows from _notes sheet
_collabs_cache= []   # rows from _collabs sheet
_ws_cache     = {}   # {title: worksheet object or _LocalWS}
DATA_CACHE_TTL = float('inf')  # never auto-expire
```

**Never read from Sheets on every request.** Instead:
- Mutations call `ws.update/append_row/delete_rows` then `patch_cache()` or `invalidate_cache()`
- `patch_cache(sheet, row, values)` — updates a single row in `_data_cache` without a network read (use after task edits)
- `invalidate_cache()` — zeroes all caches (use after notes/collabs mutations, which need a full re-read)
- The only full re-fetch is `_fetch_all()`, triggered by `GET /api/sync`

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
| POST | `/api/projects` | Create project (sheet tab) |
| DELETE | `/api/projects/<name>` | Delete project |
| POST | `/api/sync` | Force full re-fetch from Sheets |
| GET | `/api/status` | Returns `{local_mode: bool}` |

All mutation routes use `request.get_json(silent=True) or {}` — never `request.json`.

---

## Google Sheet structure

Each project tab: columns A–F

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| Deadline | Task | Hours | Status | Completed Date | Assignee |

Two hidden meta-tabs (never delete or rename):
- `_notes` — columns: Project, Note, Importance, Purpose, Color, Created, Modified
- `_collabs` — columns: Project, Name, Role

---

## Frontend (JavaScript inside the HTML string)

### Key globals
```javascript
allSheets  = []   // from /api/sheets
allTasks   = {}   // {projectName: [taskObjects]}
allNotes   = []   // flat array of note objects
allCollabs = []   // flat array of collab objects
activeSheet  = null   // null = "All Projects"
viewMode     = 'tasks' | 'upcoming' | 'notes' | 'collaborators' | 'stats' | 'procrastinate'
```

### Rendering flow
```
renderContent()
  ├── renderTasks()      — task list + garden view
  ├── renderUpcoming()   — deadline-grouped tasks
  ├── renderNotes()      — note cards
  ├── renderCollaborators()
  ├── renderStats()
  └── renderProcrastinate() — snake game + quotes
```

Navigation (`selectSheet`, `selectUpcoming`, `selectView`) always calls `renderContent()` directly using in-memory data — **never triggers a network fetch**.

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
- Tasks: add, edit, delete, inline status cycle, date picker deadline
- Bulk status change: checkbox-select multiple tasks, apply status to all
- Search: real-time filter across all projects
- Export CSV: current project or all projects
- Upcoming view: tasks grouped Overdue / Today / This Week / This Month / Later
- Stats view: overall %, hours logged, by-status breakdown, per-project bars
- Notes: add, edit, delete; importance + purpose tags; color swatches; sorted newest-first
- Collaborators: add (comma-separated for bulk), delete, assignable to tasks
- Flower progress visualization: per-project SVG flower, one petal per task
- Five color themes (Classic, Ocean, Sage, Sunset, Lavender), persisted in localStorage
- Resizable sidebar, width persisted in localStorage
- Local mode: full offline operation with `local_data.json`
- Sync button: force re-fetch from Google Sheets
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

### Note importance / purpose
Importance: `"High" | "Medium" | "Low"`  
Purpose: `"Design" | "Writing" | "Analysis" | "Planning" | "Other"`  
These map directly to CSS classes (`imp-High`, `pur-Design`, etc.) — adding new values requires adding CSS.

## Google Sheets specifics

### Rate limiting (429)
`_fetch_all()` retries up to 5 times with exponential backoff on 429 errors. The `float('inf')` TTL cache means normal usage never hits Sheets twice — only `POST /api/sync` triggers a full re-read. If you add new read paths, route them through the cache, not direct Sheets calls.

### Sheet tab names
`_notes` and `_collabs` are reserved meta-tabs. `META_SHEETS` set prevents them from appearing as projects. Any new meta-tab must be added to `META_SHEETS`.

### gspread availability
`_GSPREAD_AVAILABLE` flag — gspread is optional. The app imports it in a try/except. In local mode gspread is never called. Do not call any gspread API outside of the `if not LOCAL_MODE` paths.

## localStorage keys

| Key | Value | Set by |
|-----|-------|--------|
| `theme` | `"classic"` \| `"ocean"` \| `"sage"` \| `"sunset"` \| `"lavender"` | `applyTheme()` |
| `sidebarWidth` | integer px | sidebar resize handler |

## CSS architecture

All colours go through CSS variables on `:root`. Never hardcode a colour in CSS.  
Adding a new theme means adding an entry to the `THEMES` JS object — the CSS already uses the variables.

Status badge classes follow the pattern `status-Not\ Started`, `status-In\ Progress`, etc. (spaces escaped in CSS).  
Note badge classes: `imp-High`, `imp-Medium`, `imp-Low`, `pur-Design`, etc.

## What NOT to do

- Do not read from Sheets inside a GET route that runs on every navigation — use the cache
- Do not add `if LOCAL_MODE` branches inside API routes — put the behaviour in `_LocalWS`
- Do not use `request.json` — use `request.get_json(silent=True) or {}`
- Do not use `render_template_string` — the route returns `HTML` directly
- Do not hardcode colours in CSS — use the CSS variable system
- Do not use `escHtml()` inside JS string literals in onclick attributes — use `jsStr()`
- Do not commit `config.py`, `service_account.json`, or `local_data.json`
