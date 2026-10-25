# UI for Ghost Data Still Exists Deletion-Audit Agent

This UI lets you run the same agent logic as `code/main.py` with an interactive interface. You can:
- Run analysis for the full dataset (518 cases) and download `output.csv`
- Analyze a single deletion request via form input
- Upload custom evidence files (CSV, JSON, SQL, TXT, LOG, PDF, PNG, JPG, JPEG) along with request details and get results in the exact output format
- View results in the UI table and copy/download the JSON matching `case_id,deletion_status,steps_json`

## Quick Start

### 1) Install dependencies
```bash
cd ui
pip install -r requirements.txt
```

### 2) Run the UI
```bash
python app.py
```
Or on Windows: `run_ui.bat`. The app will start at `http://localhost:5000`.

### 3) Use it
- **Full Dataset**: Click "Run Full Dataset" to generate `output.csv` at the repo root (same as `main.py`).
- **Single Case**: Go to "Analyze Single Case", fill in `case_id`, `email`, `request_text`, `audit_at` (and optional fields), click Analyze. View steps and download JSON.
- **Custom Upload**: Go to "Upload & Analyze", upload your evidence files and provide request details. The agent will parse your uploads in-memory and analyze against them (using the existing dataset indexes for identity/rules as context).

## Notes
- The UI reuses `code/data_index.py`, `code/evidence_parser.py`, `code/audit_engine.py` directly. No logic is duplicated.
- All analysis is deterministic and respects `audit_at` cutoff.
- Output format matches the challenge exactly: `case_id,deletion_status,steps_json` with steps ordered FINDING -> REMEDIATION -> VERIFICATION.
- Secrets from `.env` are never displayed or logged.
- PDF parsing uses `pdfplumber`/`PyPDF2` if installed; images use `Pillow` if installed. The UI still works without them.

## File Structure
```
ui/
├── app.py              # Flask app
├── requirements.txt
├── run_ui.bat
├── run_ui.sh
├── README_UI.md        # This file
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── full_dataset.html
│   ├── single_case.html
│   ├── upload_analyze.html
│   └── result.html
└── static/
    ├── css/style.css
    └── js/app.js
```
