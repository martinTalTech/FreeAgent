$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  py -3 -m venv (Join-Path $PSScriptRoot ".venv")
}

& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
& $venvPython (Join-Path $PSScriptRoot "server.py")
