"""
TalTech Aineplanner local server
================================
Run:  python server.py
Open: http://localhost:8080

This server:
1. Serves index.html
2. Proxies /proxy/* requests to TalTech timetable API
3. Exposes /ai/recommend endpoint for personalized recommendations
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


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()) if len(t) >= 3]


def _clip(text: str, size: int = 220) -> str:
    t = (text or "").strip()
    if len(t) <= size:
        return t
    return t[: size - 1] + "…"


def _extract_json_object(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty LLM output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object in LLM output")
    return json.loads(text[start : end + 1])


def _heuristic_recommend(interests: str, candidates: list[dict], mandatory_courses: list[dict]) -> dict:
    tokens = _tokenize(interests)
    wants_online = any(t in {"online", "kaug", "kaugoppe", "remote", "distants"} for t in tokens)

    scored = []
    for c in candidates:
        name_blob = " ".join(
            [
                str(c.get("code", "")),
                str(c.get("name", "")),
                str(c.get("name_en", "")),
                " ".join(c.get("teachers", []) or []),
            ]
        ).lower()

        match_tokens = [t for t in tokens if t in name_blob]
        score = 0
        score += 24 if c.get("free") else 6
        kind = c.get("courseKind")
        if kind == "oma":
            score += 12
        elif kind == "teine":
            score += 6
        score += min(5, len(match_tokens)) * 11
        if wants_online and c.get("isOnline"):
            score += 6

        reason_parts = []
        if match_tokens:
            reason_parts.append(f"huvidega kattuvad teemad: {', '.join(match_tokens[:4])}")
        if c.get("free"):
            reason_parts.append("tunniplaani konflikt puudub")
        if kind == "oma":
            reason_parts.append("esineb sinu eriala kohustusliku/tugeva kursusena")
        elif kind == "teine":
            reason_parts.append("kasulik rist-erialane aine")
        if c.get("isOnline"):
            reason_parts.append("sisaldab online-opet")
        if not reason_parts:
            reason_parts.append("sobib baasvalikuks sinu tunniplaani kontekstis")

        scored.append(
            {
                "code": c.get("code", ""),
                "fit_score": max(0, min(100, score)),
                "reason": _clip("; ".join(reason_parts), 240),
                "future_value": "high" if kind in {"oma", "teine"} else "medium",
                "timetable_note": "konfliktivaba" if c.get("free") else "vajab plaani muutust",
            }
        )

    scored.sort(key=lambda x: x["fit_score"], reverse=True)
    recommended = scored[:8]
    future = [x for x in scored if x["future_value"] == "high"][:6]
    mandatory_codes = [m.get("code", "") for m in mandatory_courses if m.get("code")]

    summary = (
        f"Heuristiline soovitus {len(candidates)} kandidaadist. "
        f"Eelistasin konfliktivabu aineid, huvide tokeni-kattuvust ja eriala olulisust."
    )
    if mandatory_codes:
        summary += f" Kohustuslike ainete kontekst: {', '.join(mandatory_codes[:6])}."

    return {
        "mode": "heuristic",
        "summary": summary,
        "recommended_courses": recommended,
        "future_courses": future,
    }


def _openai_recommend(
    api_key: str,
    interests: str,
    group_code: str,
    semester: int,
    mandatory_courses: list[dict],
    candidates: list[dict],
) -> dict:
    system_prompt = (
        "You are an academic planning assistant for TalTech students. "
        "Given student interests and filtered timetable-compatible courses, "
        "return concise recommendation JSON only."
    )

    compact_candidates = []
    for c in candidates[:160]:
        compact_candidates.append(
            {
                "code": c.get("code", ""),
                "name": c.get("name", ""),
                "name_en": c.get("name_en", ""),
                "free": bool(c.get("free")),
                "time": c.get("time", ""),
                "courseKind": c.get("courseKind", ""),
                "isOnline": bool(c.get("isOnline")),
                "teachers": (c.get("teachers") or [])[:4],
            }
        )

    user_payload = {
        "student_context": {"group_code": group_code, "semester": semester, "interests": interests},
        "mandatory_courses": mandatory_courses[:20],
        "candidate_courses": compact_candidates,
        "required_response_schema": {
            "summary": "string",
            "recommended_courses": [
                {
                    "code": "string",
                    "fit_score": "number 0..100",
                    "reason": "string",
                    "future_value": "high|medium|low",
                    "timetable_note": "string",
                }
            ],
            "future_courses": [
                {
                    "code": "string",
                    "fit_score": "number 0..100",
                    "reason": "string",
                    "future_value": "high|medium|low",
                    "timetable_note": "string",
                }
            ],
        },
    }

    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }

    req = urllib.request.Request(
        OPENAI_API,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=40) as resp:
        response_data = json.loads(resp.read().decode("utf-8"))

    content = (
        response_data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    parsed = _extract_json_object(content)

    candidate_codes = {c.get("code", "") for c in candidates}

    def normalize_rows(rows: list[dict]) -> list[dict]:
        out = []
        for row in rows or []:
            code = str(row.get("code", "")).strip().upper()
            if not code or code not in candidate_codes:
                continue
            out.append(
                {
                    "code": code,
                    "fit_score": max(0, min(100, int(float(row.get("fit_score", 0))))),
                    "reason": _clip(str(row.get("reason", "")), 260),
                    "future_value": str(row.get("future_value", "medium")).lower(),
                    "timetable_note": _clip(str(row.get("timetable_note", "")), 180),
                }
            )
        return out

    recommended = normalize_rows(parsed.get("recommended_courses", []))
    future = normalize_rows(parsed.get("future_courses", []))

    if not recommended:
        raise ValueError("LLM returned no usable recommendations")

    return {
        "mode": "openai",
        "summary": _clip(str(parsed.get("summary", "")).strip(), 500),
        "recommended_courses": recommended[:10],
        "future_courses": future[:10],
    }


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
            api_path = self.path[len("/proxy") :]
            self._proxy_get(api_path)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        if self.path.startswith("/proxy/"):
            api_path = self.path[len("/proxy") :]
            self._proxy_post(api_path, body)
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

        normalized_candidates = []
        for c in candidates[:180]:
            if not isinstance(c, dict):
                continue
            code = str(c.get("code", "")).strip().upper()
            if not code:
                continue
            normalized_candidates.append(
                {
                    "code": code,
                    "name": str(c.get("name", "")).strip(),
                    "name_en": str(c.get("name_en", "")).strip(),
                    "free": bool(c.get("free")),
                    "time": str(c.get("time", "")).strip(),
                    "courseKind": str(c.get("courseKind", "")).strip(),
                    "isOnline": bool(c.get("isOnline")),
                    "teachers": c.get("allTeachers") or c.get("teachers") or [],
                }
            )

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
            normalized_mandatory.append(
                {
                    "code": code,
                    "name": str(m.get("name", "")).strip(),
                    "time": str(m.get("time", "")).strip(),
                }
            )

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if api_key:
            try:
                result = _openai_recommend(
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
                fallback = _heuristic_recommend(interests, normalized_candidates, normalized_mandatory)
                fallback["warning"] = f"OpenAI call failed, fallback used: {type(exc).__name__}"
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
        url = API_BASE + api_path
        req = urllib.request.Request(url, headers=API_HDRS, method="GET")
        self._do_proxy(req)

    def _proxy_post(self, api_path: str, body: bytes):
        url = API_BASE + api_path
        req = urllib.request.Request(url, data=body, headers=API_HDRS, method="POST")
        self._do_proxy(req)

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
    print("  TalTech Aineplanner")
    print("=" * 50)
    print(f"  Server starts on port {PORT}...")
    print(f"  Open: http://localhost:{PORT}")
    print("  Stop: Ctrl+C")
    print("=" * 50 + "\n")

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
