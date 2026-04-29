# Research Task Manager

A lightweight browser-based task manager backed by Google Sheets. Organize research projects, track deadlines, add notes, and manage collaborators — all from a clean local web UI.

![Screenshot showing task list with sidebar projects, deadline pills, and note cards]

## Features

- **Projects** — each Google Sheet tab is a project
- **Tasks** — track deadline, hours, status (Not Started / In Progress / Pending / Completed), and assignee
- **Upcoming view** — tasks grouped by deadline urgency (Overdue → Today → This Week → Later)
- **Notes** — colorful cards with importance/purpose tags, sortable by latest
- **Collaborators** — per-project team members with role labels; assignable to tasks
- **Themes** — five pastel color themes (Classic, Ocean, Sage, Sunset, Lavender)
- **Sync on demand** — data is cached locally; click **↻ Sync** to pull latest from Sheets

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/research-task-manager.git
cd research-task-manager
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your Google Sheet

1. Go to [sheets.google.com](https://sheets.google.com) and create a new spreadsheet.
2. Add one tab per project (e.g. "NeurIPS Paper", "Grant Application").
3. Copy the **Sheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/THIS_IS_YOUR_SHEET_ID/edit
   ```

### 4. Set up Google Cloud credentials

#### a) Create a GCP project

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Click **Select a project → New Project**. Give it any name.

#### b) Enable the Google Sheets API

1. In the GCP console, go to **APIs & Services → Library**.
2. Search for **Google Sheets API** and click **Enable**.

#### c) Create a Service Account

1. Go to **APIs & Services → Credentials → Create Credentials → Service Account**.
2. Give it a name (e.g. `research-tasks-sa`) and click **Create and Continue**.
3. Skip the optional role/user steps and click **Done**.

#### d) Download the key

1. Click the service account you just created.
2. Go to the **Keys** tab → **Add Key → Create new key → JSON**.
3. Save the downloaded file as `service_account.json` in this project directory.

#### e) Share the sheet with the service account

1. Open the JSON key file and copy the `client_email` field (looks like `something@project.iam.gserviceaccount.com`).
2. Open your Google Sheet → **Share** → paste the email → set role to **Editor** → click **Send**.

### 5. Configure the app

```bash
cp config.example.py config.py
```

Edit `config.py` and fill in your Sheet ID and credentials file path:

```python
SHEET_ID   = "your_sheet_id_here"
CREDS_FILE = "service_account.json"
```

### 6. Run

```bash
python app.py
```

The app opens automatically at [http://localhost:8080](http://localhost:8080).

---

## Google Sheet structure

Each project tab uses columns **A–F**:

| A        | B    | C     | D      | E              | F        |
|----------|------|-------|--------|----------------|----------|
| Deadline | Task | Hours | Status | Completed Date | Assignee |

Two hidden meta-tabs are created automatically:
- `_notes` — stores all notes across projects
- `_collabs` — stores collaborators per project

Do not delete or rename these tabs.

---

## How data syncs

| Action | Behaviour |
|--------|-----------|
| Navigate between projects | Instant — uses in-memory data, no network call |
| Add / edit / delete a task | Writes to Sheets immediately; UI updates from cache |
| Add / edit / delete a note | Writes to Sheets + re-reads notes sheet |
| Click **↻ Sync** | Forces a full re-read from Google Sheets |

---

## Tips

- **Multiple collaborators**: In the "Add Collaborator" dialog, enter comma-separated names (e.g. `Alice, Bob, Carol`).
- **Status shortcut**: Click any status badge to cycle through statuses inline.
- **Themes**: Click the colored dots in the sidebar to switch themes. Your choice is saved in localStorage.

---

## License

MIT
