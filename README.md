# TalTech Aineplaneerija 🎓
 
**AI-agent, mis koostab TalTechi tudengile konfliktivaba tunniplaani ja soovitab tema huvidele sobivaid valikaineid.**
 
🏆 **3. koht – TalTech Free Agent Hackathon (aprill 2026)**
 
![Aineplaneerija](docs/screenshot.png)
 
## Probleem
 
Iga semestri alguses peab TalTechi tudeng kokku panema tunniplaani: kohustuslikud ained on ette antud, aga valikained tuleb ise leida sadade ainete seast. Need ei tohi kohustuslikega kattuda ja võiksid ka huvidega kokku sobida. Praegu tähendab see tunniplaani lehel tundide kaupa käsitsi klikkimist ja võrdlemist.
 
## Lahendus
 
Tudeng sisestab oma õpperühma ja kirjutab vabas vormis, mis teda huvitab. Rakendus:
 
1. **loeb TalTechi tunniplaani API-st** rühma kohustuslikud ained ja laseb need üle kinnitada;
2. **leiab kõik valikained**, mis tunniplaaniga ei kattu;
3. **käivitab AI-agendi**, mis otsustab ise, kuidas soovitust koostada, ja tagastab 3–6 ainet koos põhjenduse ja prioriteediga;
4. **näitab agendi iga sammu** kasutajaliideses, nii et tudeng näeb, miks just need ained valiti.
## Kuidas agent töötab
 
Agent ei ole üks LLM-i päring, vaid **tööriistu kasutav tsükkel** (tool calling). Mudel valib ise, millist tööriista ja milliste parameetritega kutsuda:
 
| Tööriist | Mida teeb |
|---|---|
| `analyze_schedule` | Analüüsib kohustuslikke aineid: millised päevad ja kellaajad on hõivatud, millised vabad |
| `filter_courses` | Filtreerib valikaineid agendi enda valitud kriteeriumide järgi (ainult konfliktivabad, eelistatud päevad, tulemuste arv) |
| `finalize_plan` | Esitab lõpliku soovituse: valitud ained, põhjendused, sobivusskoor 0–100 ja prioriteet |
 
Tsükkel kestab kuni 8 sammu. Kui API-võti puudub, kasutab server heuristilist soovitajat, nii et demo töötab ka ilma AI-ta.
 
## Tehnoloogia
 
- **Backend:** Python (standardteegi HTTP-server), `requests`
- **AI:** Azure OpenAI (või OpenAI) – function/tool calling
- **Andmed:** TalTechi tunniplaani API (`tunniplaan-api.taltech.ee`)
- **Frontend:** üks HTML-leht, puhas JavaScript
## Meeskond
 
- **Marten Tsimolonskas** ([@matsnq](https://github.com/matsnq))
- **[nimi]** ([@mkalda](https://github.com/mkalda))
- **Martin Kaaber** ([@martinTalTech](https://github.com/martinTalTech))
Häkatonil töötasime tihedalt koos ja ülesanded liikusid jooksvalt ühelt inimeselt teisele: ideed arutati ühiselt läbi, ehitati koos valmis ja pushiti sellest kontost, kus parasjagu oli võimalik. Seetõttu ei näita commit'ide ajalugu üksikisikute panust täpselt – projekt on kogu meeskonna ühine töö.
 
Agendi tool-calling tsükli, ajafiltri ja kasutajaliidese täiendused leiab harust [`ui-polish-marten`](https://github.com/martinTalTech/FreeAgent/tree/ui-polish-marten).
 
## Käivitamine
 
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # lisa Azure OpenAI või OpenAI võti
python server.py
```
 
Ava brauseris `http://localhost:8080`.
 
Windowsis saab kasutada ka skripti `run_local.ps1`.
