# TalTech Aineplaneerija

Lokaalne prototuup, mis:
- loeb TalTechi tunniplaani API-st grupi tunnid,
- laseb kinnitada kohustuslikud ained,
- otsib konfliktivabad ained,
- lisab AI-pohise personaalse soovituse (huvide alusel).

## Kiire kaivitus (Windows / PowerShell)

```powershell
cd C:\Users\marku\IdeaProjects\FreeAgent
.\run_local.ps1
```

Ava brauseris: `http://localhost:8080`

## Kasitsi setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python server.py
```

## AI seadistus (OpenAI)

Kui tahad kasutada paris LLM-i soovitust:

```powershell
$env:OPENAI_API_KEY="sk-..."
# optional:
$env:OPENAI_MODEL="gpt-4o-mini"
.\.venv\Scripts\python server.py
```

Kui `OPENAI_API_KEY` puudub, server kasutab fallback heuristilist soovitajat,
nii et demo tootab ikkagi.

## IntelliJ/PyCharm interpreter

Vali projekti interpreteriks:

`C:\Users\marku\IdeaProjects\FreeAgent\.venv\Scripts\python.exe`
