Set-Location $PSScriptRoot

if (-not (Test-Path .venv\Scripts\python.exe)) {
  Write-Host "Creating virtual environment..."
  py -3.11 -m venv .venv
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

Write-Host "Backend: http://0.0.0.0:8000 (Ctrl+C to stop)"
& .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --loop asyncio
