$ErrorActionPreference = 'Stop'
if ((Test-Path '.venv') -and -not (Test-Path '.venv\pyvenv.cfg')) {
  Remove-Item -LiteralPath '.venv' -Recurse -Force
}
if (-not (Test-Path '.venv\Scripts\python.exe')) {
  python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -e ".[windows]"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
