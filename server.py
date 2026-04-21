"""
TalTech Aineplanner local server
================================
Run:  python server.py
Open: http://localhost:8080
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
import re
import urllib.error
import urllib.request

PORT = 8080
API_BASE = "https://tunniplaan-api.taltech.ee/api"
OPENAI_API = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

API_HDRS = {
    "Origin": "https://tunniplaan.taltech.ee",
    "Referer": "https://tunniplaan.taltech.ee/",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

DAY_NAMES = {1: "Esmaspäev", 2: "Teisipäev", 3: "Kolmapäev", 4: "Neljapäev", 5: "Reede"}
DAY_SHORT = {1: "E", 2: "T", 3: "K", 4: "N", 5: "R"}


# ── Helpers ────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()) if len(t) >= 3]


def _clip(text: str, size: int = 220) -> str:
    t = (text or "").strip()
    return t if len(t) <= size else t[: size - 1] + "…"


def _extract_json_object(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty LLM output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object in LLM output")
    return json.loads(text[start: end + 1])


def _tomin(t: str) -> int:
    if not t:
        return 0
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


# ── Heuristic fallback ─────────────────────────────────────────────────────

def _heuristic_recommend(interests: str, candidates: list[dict], mandatory_courses: list[dict]) -> dict:
    tokens = _tokenize(interests)
    wants_online = any(t in {"online", "kaug", "kaugoppe", "remote", "distants"} for t in tokens)

    scored = []
    for c in candidates:
        name_blob = " ".join([
            str(c.get("code", "")), str(c.get("name", "")),
            str(c.get("name_en", "")), " ".join(c.get("teachers", []) or []),
        ]).lower()

        match_tokens = [t for t in tokens if t in name_blob]
        score = 24 if c.get("free") else 6
        kind = c.get("courseKind")
        score += 12 if kind == "oma" else 6 if kind == "teine" else 0
        score += min(5, len(match_tokens)) * 11
        if wants_online and c.get("isOnline"):
            score += 6

        reason_parts = []
        if match_tokens:
            reason_parts.append(f"huvide kattuvus: {', '.join(match_tokens[:4])}")
        if c.get("free"):
            reason_parts.append("konfliktivaaba")
        if kind == "oma":
            reason_parts.append("eriala aine")
        if not reason_parts:
            reason_parts.append("sobib tunniplaani")

        scored.append({
            "code": c.get("code", ""),
            "fit_score": max(0, min(100, score)),
            "reason": _clip("; ".join(reason_parts), 240),
            "future_value": "high" if kind in {"oma", "teine"} else "medium",
            "timetable_note": "konfliktivaba" if c.get("free") else "konflikt",
            "priority": "recommended",
        })

    scored.sort(key=lambda x: x["fit_score"], reverse=True)
    return {
        "mode": "heuristic",
        "steps": [{"action": "heuristic_scoring", "label": "Heuristiline skoreerimine", "detail": f"Skoreerisin {len(candidates)} kandidaati huvide ja tunniplaani põhjal."}],
        "summary": f"Heuristiline soovitus {len(candidates)} kandidaadist.",
        "reasoning": "OpenAI API võti puudub — kasutasin heuristilist skoori.",
        "recommended_courses": scored[:8],
    }


# ── Agent tool implementations ─────────────────────────────────────────────

def _tool_analyze_schedule(mandatory_courses: list[dict]) -> dict:
    """Compute busy/free days and time blocks from mandatory courses."""
    busy = {d: [] for d in range(1, 6)}
    for c in mandatory_courses:
        dow = c.get("dow", -1)
        start, end = c.get("start"), c.get("end")
        if dow in busy and start and end:
            busy[dow].append({"start": start, "end": end, "course": c.get("code", "")})

    busy_days = [d for d, slots in busy.items() if slots]
    free_days = [d for d in range(1, 6) if not busy[d]]

    summary = []
    for d, slots in busy.items():
        if slots:
            times = ", ".join(f"{s['start']}–{s['end']}" for s in slots)
            summary.append(f"{DAY_SHORT[d]}: {times}")

    return {
        "busy_days": [DAY_SHORT[d] for d in busy_days],
        "free_days": [DAY_SHORT[d] for d in free_days],
        "schedule_summary": "; ".join(summary) if summary else "tundide andmed puuduvad",
        "total_mandatory_slots": sum(len(v) for v in busy.values()),
    }


def _tool_filter_candidates(candidates: list[dict], require_free: bool, prefer_days: list[str], max_results: int) -> dict:
    """Filter candidates by agent criteria."""
    result = []
    for c in candidates:
        if require_free and not c.get("free"):
            continue
        if prefer_days:
            time_str = c.get("time", "")
            day_match = any(d in time_str for d in prefer_days)
            if not day_match and c.get("dow", -1) != -1:
                continue
        result.append(c)

    return {
        "count": len(result),
        "filtered_codes": [c["code"] for c in result[:max_results]],
        "sample": [{"code": c["code"], "name": c.get("name", ""), "time": c.get("time", "")} for c in result[:8]],
    }


# ── OpenAI call helper ─────────────────────────────────────────────────────

def _openai_call(api_key: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload: dict = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        OPENAI_API,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    return data["choices"][0]["message"]


# ── Agent loop ─────────────────────────────────────────────────────────────

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_schedule",
            "description": "Analyze the student's mandatory course schedule to determine which days and times are busy vs free. Call this first.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filter_courses",
            "description": "Filter the elective candidates based on your criteria. Use after analyzing the schedule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "require_free": {"type": "boolean", "description": "Only include conflict-free courses"},
                    "prefer_days": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["E", "T", "K", "N", "R"]},
                        "description": "Preferred days for electives (free days from schedule analysis)",
                    },
                    "max_results": {"type": "integer", "description": "Max candidates to return (10-40)"},
                },
                "required": ["require_free", "prefer_days", "max_results"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_plan",
            "description": "Submit your final course recommendation plan. This ends the planning session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "decision_summary": {
                        "type": "string",
                        "description": "Short summary of what you decided and why (1-2 sentences, Estonian)",
                    },
                    "agent_reasoning": {
                        "type": "string",
                        "description": "Your full reasoning: what you analyzed, what decisions you made, why these specific courses (Estonian)",
                    },
                    "recommended_courses": {
                        "type": "array",
                        "description": "Your final course picks (3-6 courses)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "reason": {"type": "string", "description": "Why this specific course (Estonian)"},
                                "fit_score": {"type": "integer", "description": "0-100"},
                                "priority": {"type": "string", "enum": ["must-take", "recommended", "optional"]},
                            },
                            "required": ["code", "reason", "fit_score", "priority"],
                        },
                    },
                },
                "required": ["decision_summary", "agent_reasoning", "recommended_courses"],
            },
        },
    },
]


def _agent_recommend(
    api_key: str,
    interests: str,
    group_code: str,
    semester: int,
    mandatory_courses: list[dict],
    candidates: list[dict],
) -> dict:
    candidate_codes = {c["code"] for c in candidates}

    compact_candidates = [
        {
            "code": c["code"],
            "name": c.get("name", ""),
            "name_en": c.get("name_en", ""),
            "free": bool(c.get("free")),
            "time": c.get("time", ""),
            "dow": c.get("dow", -1),
            "isOnline": bool(c.get("isOnline")),
            "courseKind": c.get("courseKind", ""),
        }
        for c in candidates[:120]
    ]

    system_prompt = (
        "Sa oled autonoomne õppekava planeerimise agent TalTech tudengitele. "
        "Sinu ülesanne on ISESEISV otsustamine — mitte lihtsalt nimekirja koostamine. "
        "Kasuta tööriistu järgmises järjestuses:\n"
        "1. analyze_schedule — vaata mis päevad/ajad on hõivatud\n"
        "2. filter_courses — otsusta ise milliseid aineid vaadata (vabad päevad, konfliktivabasus)\n"
        "3. finalize_plan — esita konkreetne plaan koos põhjendustega\n\n"
        "Otsusta ise: mitu ainet soovitad, mis prioriteediga, miks just need. "
        "Kirjuta eesti keeles. Ole konkreetne, mitte üldine."
    )

    user_message = (
        f"Grupp: {group_code}, Semester: {semester}\n"
        f"Huvid: {interests or 'täpsustamata'}\n"
        f"Kohustuslikud ained: {json.dumps([{'code': m['code'], 'name': m.get('name',''), 'time': m.get('time','')} for m in mandatory_courses[:15]], ensure_ascii=False)}\n"
        f"Valikainete kandidaadid ({len(compact_candidates)} tk): {json.dumps(compact_candidates, ensure_ascii=False)}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    steps = []
    final_plan = None
    step_labels = {
        "analyze_schedule": "Analüüsin tunniplaani",
        "filter_courses": "Filtreerin kandidaadid",
        "finalize_plan": "Koostan lõpliku plaani",
    }

    for _ in range(8):
        msg = _openai_call(api_key, messages, AGENT_TOOLS)
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            break

        messages.append(msg)

        for tc in tool_calls:
            func_name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}

            if func_name == "analyze_schedule":
                result = _tool_analyze_schedule(mandatory_courses)
                detail = (
                    f"Hõivatud päevad: {', '.join(result['busy_days']) or 'puuduvad'}. "
                    f"Vabad päevad: {', '.join(result['free_days']) or 'puuduvad'}. "
                    f"{result['schedule_summary']}"
                )
                steps.append({"action": func_name, "label": step_labels[func_name], "detail": detail})

            elif func_name == "filter_courses":
                result = _tool_filter_candidates(
                    candidates,
                    require_free=bool(args.get("require_free", True)),
                    prefer_days=args.get("prefer_days", []),
                    max_results=int(args.get("max_results", 30)),
                )
                detail = (
                    f"Leidsin {result['count']} sobivat kandidaati "
                    f"(eelistatud päevad: {', '.join(args.get('prefer_days', [])) or 'kõik'}). "
                    f"Vaatan lähemalt: {', '.join(result['filtered_codes'][:6])}{'…' if len(result['filtered_codes']) > 6 else ''}."
                )
                steps.append({"action": func_name, "label": step_labels[func_name], "detail": detail})

            elif func_name == "finalize_plan":
                # Validate codes
                valid_courses = []
                for course in args.get("recommended_courses", []):
                    code = str(course.get("code", "")).strip().upper()
                    if code in candidate_codes:
                        valid_courses.append({
                            "code": code,
                            "reason": _clip(str(course.get("reason", "")), 260),
                            "fit_score": max(0, min(100, int(float(course.get("fit_score", 70))))),
                            "priority": str(course.get("priority", "recommended")),
                            "future_value": "high" if course.get("priority") == "must-take" else "medium",
                            "timetable_note": "konfliktivaba",
                        })
                result = {"accepted": len(valid_courses)}
                steps.append({
                    "action": func_name,
                    "label": step_labels[func_name],
                    "detail": f"Valisin {len(valid_courses)} ainet. {_clip(args.get('decision_summary',''), 160)}",
                })
                final_plan = {
                    "summary": _clip(str(args.get("decision_summary", "")), 400),
                    "reasoning": _clip(str(args.get("agent_reasoning", "")), 800),
                    "recommended_courses": valid_courses[:8],
                }

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

        if final_plan:
            break

    if not final_plan:
        raise ValueError("Agent did not produce a final plan")

    return {
        "mode": "agent",
        "steps": steps,
        "summary": final_plan["summary"],
        "reasoning": final_plan["reasoning"],
        "recommended_courses": final_plan["recommended_courses"],
    }


# ── HTTP Handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path in {"/", "/index.html"}:
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        if self.path.startswith("/proxy/"):
            self._proxy_get(self.path[len("/proxy"):])
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        if self.path.startswith("/proxy/"):
            self._proxy_post(self.path[len("/proxy"):], body)
            return
        if self.path == "/ai/recommend":
            self._handle_ai_recommend(body)
            return

        self.send_response(404)
        self.end_headers()

    def _handle_ai_recommend(self, body: bytes):
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        interests = str(req.get("interests", "")).strip()
        candidates = req.get("candidates") or []
        mandatory_courses = req.get("mandatory_courses") or []
        group_code = str(req.get("group_code", "")).strip().upper()
        semester = int(req.get("semester", 1) or 1)

        if not isinstance(candidates, list) or not candidates:
            self._send_json(400, {"error": "candidates must be a non-empty array"})
            return

        def normalize_candidate(c):
            if not isinstance(c, dict):
                return None
            code = str(c.get("code", "")).strip().upper()
            if not code:
                return None
            return {
                "code": code,
                "name": str(c.get("name", "")).strip(),
                "name_en": str(c.get("name_en", "")).strip(),
                "free": bool(c.get("free")),
                "time": str(c.get("time", "")).strip(),
                "dow": int(c.get("dow", -1)),
                "courseKind": str(c.get("courseKind", "")).strip(),
                "isOnline": bool(c.get("isOnline")),
                "teachers": c.get("allTeachers") or c.get("teachers") or [],
            }

        normalized_candidates = [r for c in candidates[:180] if (r := normalize_candidate(c))]
        if not normalized_candidates:
            self._send_json(400, {"error": "No usable candidates"})
            return

        normalized_mandatory = []
        for m in mandatory_courses[:30]:
            if not isinstance(m, dict):
                continue
            code = str(m.get("code", "")).strip().upper()
            if not code:
                continue
            normalized_mandatory.append({
                "code": code,
                "name": str(m.get("name", "")).strip(),
                "time": str(m.get("time", "")).strip(),
                "dow": int(m.get("dow", -1)),
                "start": str(m.get("start", "")),
                "end": str(m.get("end", "")),
            })

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if api_key:
            try:
                result = _agent_recommend(
                    api_key=api_key,
                    interests=interests,
                    group_code=group_code,
                    semester=semester,
                    mandatory_courses=normalized_mandatory,
                    candidates=normalized_candidates,
                )
                self._send_json(200, result)
                return
            except Exception as exc:
                print(f"  [Agent error] {type(exc).__name__}: {exc}")
                fallback = _heuristic_recommend(interests, normalized_candidates, normalized_mandatory)
                fallback["warning"] = f"Agent failed, fallback used: {type(exc).__name__}"
                self._send_json(200, fallback)
                return

        self._send_json(200, _heuristic_recommend(interests, normalized_candidates, normalized_mandatory))

    def _serve_file(self, filename: str, ctype: str):
        try:
            path = os.path.join(os.path.dirname(__file__), filename)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _proxy_get(self, api_path: str):
        self._do_proxy(urllib.request.Request(API_BASE + api_path, headers=API_HDRS, method="GET"))

    def _proxy_post(self, api_path: str, body: bytes):
        self._do_proxy(urllib.request.Request(API_BASE + api_path, data=body, headers=API_HDRS, method="POST"))

    def _do_proxy(self, req: urllib.request.Request):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_cors()
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_cors()
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.send_cors()
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)

    print("\n" + "=" * 50)
    print("  TalTech Aineplanner — AI Agent")
    print("=" * 50)
    print(f"  Avatud: http://localhost:{PORT}")
    print(f"  OpenAI võti: {'OK ✓' if os.getenv('OPENAI_API_KEY') else 'puudub (heuristika)'}")
    print("  Stopp: Ctrl+C")
    print("=" * 50 + "\n")

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
