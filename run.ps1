$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" app.py --open
