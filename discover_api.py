"""
TalTech Timetable API Endpoint Discovery
=========================================
Run this FIRST to find the correct endpoint for fetching group timetable events.
Open tunniplaan.taltech.ee in browser, select any group (e.g. IABB11),
then paste the XHR URL from Network tab when prompted.

OR just run this and it will try common patterns automatically.
"""

import requests
import json

TIMETABLE_API = "https://tunniplaan-api.taltech.ee/api"
TIMETABLE_ID  = 10
HEADERS = {
    "Origin": "https://tunniplaan.taltech.ee",
    "Referer": "https://tunniplaan.taltech.ee/",
    "Accept": "application/json",
}

# Known group ID from departments response (IADB40A = 15906)
TEST_GROUP_ID = 15906
TEST_GROUP_CODE = "IADB40A"

def try_endpoint(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        print(f"  {r.status_code}  {url}")
        if r.status_code == 200:
            data = r.json()
            print(f"         ✓ Got {len(str(data))} chars, keys: {list(data.keys()) if isinstance(data, dict) else 'list['+str(len(data))+']'}")
            return data
    except Exception as e:
        print(f"  ERR  {url}  ({e})")
    return None

print("=== Trying common endpoint patterns ===\n")

candidates = [
    f"{TIMETABLE_API}/timetable/student-group/{TEST_GROUP_ID}?ttId={TIMETABLE_ID}",
    f"{TIMETABLE_API}/student-group/{TEST_GROUP_ID}?ttId={TIMETABLE_ID}",
    f"{TIMETABLE_API}/events?ttId={TIMETABLE_ID}&groupId={TEST_GROUP_ID}",
    f"{TIMETABLE_API}/lessons?ttId={TIMETABLE_ID}&groupId={TEST_GROUP_ID}",
    f"{TIMETABLE_API}/schedule?ttId={TIMETABLE_ID}&groupId={TEST_GROUP_ID}",
    f"{TIMETABLE_API}/timetable?ttId={TIMETABLE_ID}&groupId={TEST_GROUP_ID}",
    f"{TIMETABLE_API}/timetable?ttId={TIMETABLE_ID}&studentGroupId={TEST_GROUP_ID}",
    f"{TIMETABLE_API}/student-group/{TEST_GROUP_ID}/timetable?ttId={TIMETABLE_ID}",
    f"{TIMETABLE_API}/public/{TEST_GROUP_ID}?ttId={TIMETABLE_ID}",
    f"{TIMETABLE_API}/group/{TEST_GROUP_CODE}?ttId={TIMETABLE_ID}",
    f"{TIMETABLE_API}/timetable/{TEST_GROUP_CODE}?ttId={TIMETABLE_ID}",
]

found = None
for url in candidates:
    result = try_endpoint(url)
    if result:
        found = (url, result)
        break

print()
if found:
    url, data = found
    print(f"✓ WORKING ENDPOINT: {url}")
    print()
    print("First 500 chars of response:")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:500])
    print()
    print("→ Copy the endpoint pattern into scraper.py fetch_group_timetable()")
else:
    print("✗ None of the common patterns worked.")
    print()
    print("Manual step needed:")
    print("1. Open https://tunniplaan.taltech.ee in browser")
    print("2. Open DevTools → Network → Fetch/XHR")
    print("3. Search for group 'IABB11' in the timetable UI")
    print("4. Look for a new XHR request that appears")
    print("5. Copy that URL and paste it here:")
    print()
    manual_url = input("Paste URL (or press Enter to skip): ").strip()
    if manual_url:
        result = try_endpoint(manual_url)
        if result:
            print("\nResponse structure:")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:1000])
