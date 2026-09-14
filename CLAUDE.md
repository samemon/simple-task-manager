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
All mutation routes call methods on a worksheet object (`ws.update(...)`, `ws.append_row(...)`, `ws.delete_rows(...)`). In local mode, `_LocalWS` provides the same interface backed by the in-memory caches + `_local_save()`. This means **all API routes work unchanged in both modes** — never add mode-specific branches inside a route; put them in `_LocalWS` instead. (`append_meta_cache`/`delete_meta_cache_row` are a narrow, deliberate exception — see below — because those two operations are not idempotent and `_LocalWS` already applies them internally.)

### In-memory cache
```
_data_cache   = {}   # {project_name: [rows]}
_notes_cache  = []   # rows from _notes sheet
_collabs_cache= []   # rows from _collabs sheet
_lit_cache    = []   # rows from _literature sheet
_ws_cache     = {}   # {title: worksheet object or _LocalWS}
DATA_CACHE_TTL = float('inf')  # never auto-expire
```

**Never read from Sheets on every request, and never force a full re-fetch for a single-row edit.** Every mutation patches its own cache array in place instead:
- Tasks: `ws.update(...)` then `patch_cache(sheet, row, values)` — updates one row in `_data_cache` without a network read. Safe to call even though `_LocalWS.update()` already patches the same cache internally (idempotent — same row, same values).
- Notes/collabs/literature: `ws.update(...)` then `patch_meta_cache(cache_list, row, values)` (same idempotent-so-always-safe pattern). For `ws.append_row(...)` and `ws.delete_rows(...)`, use `append_meta_cache(cache_list, ws, values)` / `delete_meta_cache_row(cache_list, ws, row)` instead — these two are **not** idempotent (append would duplicate the row, delete would remove an extra one), and `_LocalWS.append_row()`/`.delete_rows()` already mutate the cache as part of local-mode persistence, so the helper is a no-op there and only touches the cache for the real-Sheets `ws` (checked via `isinstance(ws, _LocalWS)`).
- `invalidate_cache()` — zeroes all caches, forcing the next read to do a full `_fetch_all()` re-fetch of every worksheet. Reserved for `POST /api/sync` only — **do not call it from a mutation route**; a burst of `invalidate_cache()`-then-refetch calls (e.g. scripting several edits back to back) can exhaust Google's per-minute read quota and start returning 500s, which is exactly what patch/append/delete-in-place avoids.

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

Three hidden meta-tabs (never delete or rename):
- `_notes` — columns: Project, Note, Importance, Purpose, Color, Created, Modified
- `_collabs` — columns: Project, Name, Role
- `_literature` — columns: Project, Title, Link, Authors, Year, Notes, Created, Modified

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
  ├── renderUpcoming()   — deadline-grouped tasks
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
- Upcoming view: tasks grouped Overdue / Today / This Week / This Month / Later
- Plan for Today: pinned section above Overdue in the Upcoming view — drag any task row onto it, or click its `+` to search-and-add a task, to build a daily working list independent of deadline. Stored client-side (`localStorage`), resets automatically each calendar day
- Stats view: overall %, hours logged, by-status breakdown, per-project bars
- Notes: rich text editor (bold/italic/underline/bullets/numbered lists), title, font family (5), font size; importance + purpose tags; color swatches; sorted newest-first
- Literature: references grouped into a colored card per project (same visual language as GarDone/Garden — top border + tinted header use the project's `FLOWER_DEFS` accent, plus a small bloomed flower icon with one petal per reference), laid out two cards per row with each card's reference list independently scrollable (`.lit-card-body`, `max-height` + `overflow-y`); each row shows the title as a clickable link (URL itself hidden), a 🗒 button that toggles a hidden notes/annotation panel, edit and delete; add/edit via modal (title, link, authors, year, notes); project filter dropdown + title search box (`#l-search`) — the toolbar is only built once per view-entry (guarded by `if (!document.getElementById('lit-toolbar'))`) so retyping in the search box never rebuilds and steals focus from itself, it just re-renders `#lit-cards-wrap` via `renderLiteratureCards()`; client-side `.bib` export (per-project or all) for import into Zotero/reference managers
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
| `projectOrder` | JSON array of project names | sidebar drag-to-reorder |
| `demoMode` | `"1"` \| `"0"` | `toggleDemoMode()` |
| `todayPlan` / `todayPlanDemo` | `{date: "YYYY-MM-DD", keys: ["sheet::row", ...]}` | `saveTodayPlan()` — the `Demo` variant is used while `demoMode` is on, so pins don't bleed between real and demo data; a stored `date` other than today is discarded on next read |

## CSS architecture

All colours go through CSS variables on `:root`. Never hardcode a colour in CSS.  
Adding a new theme means adding an entry to the `THEMES` JS object — the CSS already uses the variables.

Status badge classes follow the pattern `status-Not\ Started`, `status-In\ Progress`, etc. (spaces escaped in CSS).  
Note badge classes: `imp-High`, `imp-Medium`, `imp-Low`, `pur-Design`, etc.

## Demo mode architecture

`DEMO_DATA` is a JS constant (baked into the HTML string) containing 14 projects, 52 tasks, 47 collaborators, 2 notes, and 2 literature references — matching the rough scale of real data.

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
