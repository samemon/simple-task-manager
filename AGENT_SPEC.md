# Research Task Manager — Agent Build Spec

This document is written for an AI agent. Use it to build a personal task manager for a researcher (or adapt it to any knowledge worker). The goal is not to replicate code exactly but to understand what the app does, why each feature exists, and build the best version you can for the user's specific context.

---

## The problem this solves

Researchers juggle many parallel projects (papers, grants, datasets, reviews, talks) with hard deadlines, changing collaborators, and irregular working hours. Standard to-do apps are too generic. Spreadsheets are powerful but clunky. This app sits in the middle: structured enough to be useful, simple enough to actually use.

Key pain points it addresses:
- Forgetting which tasks are due soon across all projects
- Losing track of notes and insights tied to a specific paper or experiment
- Not knowing who is responsible for what
- No sense of progress on a project at a glance
- Switching between many apps for tasks, notes, and people

---

## Core principles

1. **Local-first** — must work without any cloud account. Data should live on the user's machine by default.
2. **No login friction** — open it, it works. No accounts, no sync setup required to get started.
3. **One window** — everything in a single browser tab served from localhost. No switching between apps.
4. **Instant navigation** — switching between projects must be instant (use in-memory data, not network calls on every click).
5. **Researcher-aware** — deadlines, hours, collaborators, and notes are first-class concepts, not afterthoughts.

---

## Features to build

Build all of these unless the user explicitly says otherwise. Each section explains what it is and why it matters.

### Projects
Each project is a named container for tasks, notes, and collaborators. Examples: "NeurIPS 2026 Paper", "NSF Grant Proposal", "Lab Website".

- User can create and delete projects
- Sidebar lists all projects with a count of active (non-completed) tasks
- Clicking a project shows only that project's content

**Why:** researchers think in projects, not in flat task lists.

### Tasks
Each task has: description, deadline, hours estimate, status, assignee, and completed date.

Statuses: `Not Started`, `In Progress`, `Pending`, `Completed`

- Add, edit, delete tasks
- Clicking a status badge cycles through statuses inline (no modal needed)
- Completed date is auto-set to today when a task is marked complete
- Deadline shown as a coloured pill (red = overdue, orange = today, yellow = this week, blue = later)

**Why:** hours estimates help researchers plan. Status cycling without opening a modal saves clicks.

### Upcoming view
A cross-project view that groups all non-completed tasks by deadline urgency:
- **Overdue** — past deadline
- **Today** — due today
- **This Week** — due in the next 7 days
- **This Month** — due in the next 30 days
- **Later** — everything else

Sorted by deadline within each group.

**Why:** the most important daily view. Shows what needs attention right now, across all projects.

### Notes
Freeform text notes attached to a project. Each note has: text, importance (High / Medium / Low), purpose tag (e.g. Finding, Insight, Concern, Reminder), creation date, and a color.

- Add, edit, delete notes
- Sort newest-first
- Filter by project, importance, or purpose
- Show a preview of recent notes on the project task view

**Why:** researchers constantly generate insights, reviewer concerns, advisor feedback, and experimental findings. These need to live next to the tasks they relate to, not in a separate notes app.

### Collaborators
Each project has a list of collaborators with names and role labels (e.g. "Lead author", "RA", "Advisor").

- Add multiple collaborators at once (comma-separated names)
- Assign collaborators to tasks
- View who is working on what

**Why:** research is collaborative. Knowing who owns a task is essential.

### Stats view
A dashboard showing:
- Overall progress (% complete across all tasks)
- Total hours logged and hours in completed work
- Task count by status (Completed, In Progress, Pending, Not Started)
- Per-project progress bar

**Why:** gives a sense of momentum and helps identify which projects are stalling.

### Search
Real-time search across task names, assignees, deadlines, and statuses.

**Why:** when you have 10+ projects and 100+ tasks, you need to find things fast.

### Bulk status change
Checkboxes on tasks + a floating action bar to change status on multiple tasks at once.

**Why:** at the end of a work session, marking 8 tasks complete one by one is painful.

### Export CSV
Download all tasks (or just the current project) as a CSV file.

**Why:** researchers need to share progress with advisors, include in reports, or do their own analysis.

### Progress visualization
Each project gets a unique visual indicator of completion — ideally something more engaging than a plain progress bar. The reference implementation uses SVG flowers where each petal represents a task and fills with color on completion.

Feel free to choose a different visual if it suits the user better (e.g. progress rings, bar charts, emoji-based indicators).

**Why:** seeing a half-bloomed flower is more motivating than "4/9 complete". Small UX detail, real psychological effect.

### Procrastination break tab
A dedicated space for a timed break. The reference implementation includes:
- A snake game
- A configurable timer (1–5 minutes)
- A panel of deep, substantive quotes from thinkers the user respects
- A "time's up" overlay with a humorous message when the timer runs out

Ask the user which thinkers/authors they'd like quotes from, or use a general selection of researchers, philosophers, and writers. Do not use shallow motivational quotes.

**Why:** breaks are necessary. A built-in, time-boxed break removes guilt and brings the user back to work.

### Themes
At least 3–5 color themes the user can switch between. Persist the choice across sessions.

**Why:** people spend hours in this app. Aesthetic preference matters for sustained use.

---

## Technical recommendations

These are recommendations, not requirements. Use your judgment for what fits the user's context.

### Stack
- **Backend:** Python + Flask (lightweight, easy to run locally, no build step)
- **Frontend:** Vanilla JS + plain CSS served as a string from Flask (no build toolchain, no npm, no bundler)
- **Storage:** JSON file by default; optionally connect to Google Sheets or another backend if the user wants cloud sync

### Single-file preferred
Keeping the backend and frontend in one file (`app.py`) makes the app easy to understand, share, and modify. Avoid splitting into many files unless the user specifically requests it.

### Local-first data flow
- Load all data into memory on startup
- Serve navigation and filtering from the in-memory cache (instant)
- Write mutations immediately to storage
- Only re-read from storage when the user explicitly requests a sync

### Launcher scripts
Provide double-click launchers for macOS (`start.command`) and Windows (`start.bat`) that:
1. Check Python is installed
2. Create a virtual environment if it doesn't exist
3. Install dependencies
4. Start the server

**macOS note:** the `.command` file will be blocked by Gatekeeper on first run. The user must go to System Settings → Privacy & Security → Open Anyway. Document this clearly.

### No authentication
This runs on localhost. No login, no sessions, no tokens. The user is always the only user.

### Dependency list
Minimal:
```
flask>=3.0
gspread>=6.0       # only if Google Sheets integration is wanted
google-auth>=2.0   # only if Google Sheets integration is wanted
```

---

## What to ask the user before building

1. Do you want cloud sync (Google Sheets) or purely local storage?
2. Which projects will you track? (Helps seed the data model with real examples)
3. Who are the collaborators you work with most?
4. Which thinkers or authors do you want in the quotes panel?
5. Do you have a preferred color aesthetic?
6. Are there any features in the list above you don't need?
7. Is there anything important to your workflow that's missing from this spec?

---

## What to skip (unless asked)

- User accounts / authentication
- Mobile responsiveness (desktop-first is fine)
- Real-time multi-user collaboration
- Subtasks or task dependencies (complex schema change, low return)
- Recurring tasks (same reason)
- Email/calendar integration
- AI-generated task suggestions
- Any feature that requires an always-on server or cloud account to function at all

---

## Definition of done

The app is complete when:
- All features in the "Features to build" section work end-to-end
- Data persists across restarts
- A non-technical user can get it running with a double-click (after Python is installed)
- The README explains setup clearly, including the macOS Gatekeeper step
