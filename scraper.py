"""
TalTech Course Conflict Checker — scraper.py
============================================
OIS2 blokeerib otse-päringuid (403), seega tudeng sisestab
kohustuslikud ained ise (need on ÕIS-ist teada).
Süsteem kontrollib konflikte timetable API vastu.

Käivita:  python scraper.py
"""

import requests, json
from dataclasses import dataclass, asdict

TIMETABLE_API = "https://tunniplaan-api.taltech.ee/api"
TIMETABLE_ID  = 10
HEADERS = {
    "Origin":       "https://tunniplaan.taltech.ee",
    "Referer":      "https://tunniplaan.taltech.ee/",
    "Accept":       "application/json",
    "Content-Type": "application/json",
}

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class TimeSlot:
    dow:        int    # 1=E 2=T 3=K 4=N 5=R  -1=pole fikseeritud aega
    start:      str    # "14:00"
    end:        str    # "15:30"
    week_codes: list   # ["NADAL_1", ...]

@dataclass
class CourseEvent:
    course_code: str
    course_name: str
    event_type:  str
    slot:        TimeSlot

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_time(s):
    if not s: return "", ""
    p = s.strip().split(" - ")
    return (p[0].strip(), p[1].strip()) if len(p) == 2 else ("", "")

def to_min(t):
    if not t: return 0
    h, m = t.split(":")
    return int(h)*60 + int(m)

def slots_overlap(a: TimeSlot, b: TimeSlot) -> bool:
    if a.dow == -1 or b.dow == -1: return False
    if a.dow != b.dow: return False
    if not a.start or not b.start: return False
    if a.week_codes and b.week_codes:
        if not set(a.week_codes) & set(b.week_codes): return False
    return to_min(a.start) < to_min(b.end) and to_min(b.start) < to_min(a.end)

# ── API calls ─────────────────────────────────────────────────────────────────

def fetch_all_groups():
    url  = f"{TIMETABLE_API}/public/departments?ttId={TIMETABLE_ID}"
    data = requests.get(url, headers=HEADERS, timeout=15).json()
    groups = []
    for dept in data.get("departments", []):
        for curr in dept.get("curriculums", []):
            for sg in curr.get("studentGroups", []):
                groups.append({
                    "group_code":            sg["code"],
                    "sg_id":                 sg["id"],
                    "curriculum_version_id": curr.get("curriculumVersionId") or curr.get("id"),
                    "department_id":         dept.get("departmentId"),
                    "curriculum_code":       curr.get("code", ""),
                    "curriculum_name_en":    curr.get("nameEn", ""),
                })
    return groups

def fetch_timetable(sg_id, curriculum_version_id, department_id):
    url, events, page = f"{TIMETABLE_API}/public/search", [], 1
    while True:
        payload = {
            "ttId": TIMETABLE_ID, "sgId": sg_id,
            "curriculumVersionId": curriculum_version_id,
            "departmentId": department_id,
            "mode": "OTHER", "page": page, "size": 50,
        }
        data  = requests.post(url, headers=HEADERS, json=payload, timeout=15).json()
        days  = data.get("weekDays", [])
        if not days: break
        for day in days:
            for row in day.get("rows", []):
                s, e = parse_time(row.get("time") or "")
                events.append(CourseEvent(
                    course_code = row.get("subjectCode", ""),
                    course_name = row.get("subjectName", ""),
                    event_type  = row.get("type", ""),
                    slot        = TimeSlot(row.get("dow",-1), s, e, row.get("weekCodes",[])),
                ))
        if sum(len(d.get("rows",[])) for d in days) < 50: break
        page += 1
    return events

def find_free_electives(mandatory_events, all_events, elective_codes):
    """Return elective course codes with zero time conflicts."""
    free = []
    for code in elective_codes:
        e_events = [e for e in all_events if e.course_code == code]
        if not e_events:
            free.append(code)   # not in timetable = no conflict
            continue
        conflict = any(slots_overlap(e.slot, m.slot)
                       for e in e_events for m in mandatory_events)
        if not conflict:
            free.append(code)
    return free

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== TalTech Course Conflict Checker ===\n")

    # 1. Lae kõik grupid
    print("1. Laen kõik grupid...")
    groups = fetch_all_groups()
    with open("groups.json", "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    print(f"   {len(groups)} gruppi salvestatud → groups.json\n")

    # 2. Vali grupp
    TARGET = "IAAB21"   # ← MUUDA SIIA OMA GRUPI KOOD
    group  = next((g for g in groups if g["group_code"] == TARGET), None)
    if not group:
        print(f"   VIGA: gruppi '{TARGET}' ei leitud!")
        print("   Saadaval:", [g["group_code"] for g in groups[:15]])
        exit(1)

    # 3. Lae tunniplaan
    print(f"2. Laen tunniplaani grupile {TARGET}...")
    events = fetch_timetable(group["sg_id"], group["curriculum_version_id"], group["department_id"])
    with open("timetable.json", "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in events], f, ensure_ascii=False, indent=2)
    print(f"   {len(events)} tundi salvestatud → timetable.json\n")
    print("   Tunniplaan:")
    for e in events:
        if e.slot.dow != -1:
            days = {1:"E",2:"T",3:"K",4:"N",5:"R"}
            print(f"   {days.get(e.slot.dow,'?')}  {e.slot.start}-{e.slot.end}  {e.course_code}  {e.course_name[:40]}")

    # 4. Sisesta kohustuslikud ained
    print("\n3. Sisesta oma kohustuslikud aine koodid (need leiad ÕIS-ist)")
    print("   Vajuta Enter ilma koodita kui oled lõpetanud.\n")

    mandatory_codes = []
    while True:
        code = input("   Kohustuslik aine kood (nt ICY0030): ").strip().upper()
        if not code:
            break
        mandatory_codes.append(code)
        print(f"   ✓ Lisatud: {code}")

    if not mandatory_codes:
        print("\n   Kohustuslikke aineid ei sisestatud. Kasutan kõiki tunniplaani aineid.")
        mandatory_codes = list(set(e.course_code for e in events if e.slot.dow != -1))

    mandatory_events = [e for e in events if e.course_code in mandatory_codes]
    print(f"\n   {len(mandatory_events)} kohustusliku aine tundi tunniplaanist\n")

    # 5. Sisesta valikained
    print("4. Sisesta valikained mida soovid kontrollida")
    print("   (need leiad ÕIS-ist õppekava valikainete alt)\n")

    elective_codes = []
    while True:
        code = input("   Valikaine kood (nt ICA0017): ").strip().upper()
        if not code:
            break
        elective_codes.append(code)
        print(f"   ✓ Lisatud: {code}")

    if not elective_codes:
        print("   Valikaineid ei sisestatud.")
        exit(0)

    # 6. Kontrolli konflikte
    print(f"\n5. Kontrollin {len(elective_codes)} valikaine konflikte...\n")

    # Lae valikainete tunniplaan kõigist gruppidest
    all_group_events = list(events)   # alusta oma grupist

    # Otsi valikained teistest gruppidest kui omas ei leidu
    missing = [c for c in elective_codes if not any(e.course_code == c for e in events)]
    if missing:
        print(f"   Otsin {len(missing)} valikaine tunnid teistest gruppidest...")
        for g in groups[:30]:   # kontrolli esimest 30 gruppi
            if g["group_code"] == TARGET:
                continue
            g_events = fetch_timetable(g["sg_id"], g["curriculum_version_id"], g["department_id"])
            for code in missing:
                if any(e.course_code == code for e in g_events):
                    all_group_events.extend(g_events)
                    missing.remove(code)
                    print(f"   Leidsin {code} grupist {g['group_code']}")
            if not missing:
                break

    free = find_free_electives(mandatory_events, all_group_events, elective_codes)
    conflict = [c for c in elective_codes if c not in free]

    print(f"\n{'='*50}")
    print(f"✓ SOBIVAD valikained ({len(free)}):")
    for code in free:
        ev = next((e for e in all_group_events if e.course_code == code and e.slot.dow != -1), None)
        if ev:
            days = {1:"E",2:"T",3:"K",4:"N",5:"R"}
            print(f"   {code}  {days.get(ev.slot.dow,'?')} {ev.slot.start}-{ev.slot.end}  {ev.course_name[:45]}")
        else:
            print(f"   {code}  (pole fikseeritud aega — pole konflikti)")

    if conflict:
        print(f"\n✗ KONFLIKTSED valikained ({len(conflict)}):")
        for code in conflict:
            ev = next((e for e in all_group_events if e.course_code == code and e.slot.dow != -1), None)
            m  = next((e for e in mandatory_events if ev and slots_overlap(e.slot, ev.slot)), None)
            if ev and m:
                days = {1:"E",2:"T",3:"K",4:"N",5:"R"}
                print(f"   {code}  {days.get(ev.slot.dow,'?')} {ev.slot.start}-{ev.slot.end}  ← konflikt: {m.course_code} {m.slot.start}-{m.slot.end}")
            else:
                print(f"   {code}")
    print('='*50)
