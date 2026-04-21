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

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import unescape as html_unescape
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def _load_env_file(path: str):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key:
                    continue
                # Shell env has priority over file-based defaults.
                if key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # Keep server startup resilient; AI will fall back with a warning.
        pass


def _load_local_env_files():
    base = os.path.dirname(os.path.abspath(__file__))
    # .env.local is preferred over .env for local secret overrides.
    _load_env_file(os.path.join(base, ".env.local"))
    _load_env_file(os.path.join(base, ".env"))


_load_local_env_files()

PORT = 8080
API_BASE = "https://tunniplaan-api.taltech.ee/api"
OPENAI_API = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
AZURE_OPENAI_ENDPOINT = (
    os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    or os.getenv("AZURE_API_BASE", "").strip()
).rstrip("/")
AZURE_OPENAI_API_VERSION = (
    os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
    or os.getenv("AZURE_API_VERSION", "").strip()
    or "2024-10-21"
)
AZURE_OPENAI_DEPLOYMENT = (
    os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
    or os.getenv("AZURE_DEPLOYMENT", "").strip()
)
OIS_SUBJECT_BASE = "https://ois2.taltech.ee/uusois/subject"

API_HDRS = {
    "Origin": "https://tunniplaan.taltech.ee",
    "Referer": "https://tunniplaan.taltech.ee/",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

COURSE_PROFILE_CACHE: dict[str, dict] = {}
SUBJECT_ID_CACHE: dict[str, int | None] = {}
COURSE_CODE_PATTERNS = (
    r"\b[A-Z]{2,6}\d{3,4}[A-Z]?\b",
    r"\b[A-Z]{2,6}\.\d{2}\.\d{3}\b",
)
TOKEN_STOPWORDS = {
    "and",
    "ning",
    "with",
    "for",
    "the",
    "that",
    "this",
    "from",
    "have",
    "more",
    "less",
    "want",
    "would",
    "could",
    "mind",
    "mina",
    "huvitab",
    "olen",
    "tahan",
    "soovin",
    "about",
    "teha",
    "saada",
    "mingi",
    "sest",
    "väga",
    "vaga",
    "siis",
    "kuna",
    "nagu",
}


def _tokenize(text: str) -> list[str]:
    out = []
    seen = set()
    for t in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()):
        if len(t) < 3 or t in TOKEN_STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _clip(text: str, size: int = 220) -> str:
    t = (text or "").strip()
    if len(t) <= size:
        return t
    return t[: size - 3] + "..."


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


def _http_get_text(url: str, headers: dict | None = None, timeout: int = 20) -> str:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _http_get_json(url: str, headers: dict | None = None, timeout: int = 20) -> dict | list:
    return json.loads(_http_get_text(url, headers=headers, timeout=timeout))


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 40) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        if detail:
            raise ValueError(f"HTTP {e.code}: {_clip(detail, 420)}")
        raise ValueError(f"HTTP {e.code}: {e.reason}")


def _rtf_to_text(rtf: str) -> str:
    text = (rtf or "").replace("\r", "\n")
    text = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", errors="ignore"),
        text,
    )
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\tab", "\t", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = html_unescape(text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _extract_course_description(raw_text: str) -> str:
    lines = [ln.strip() for ln in (raw_text or "").splitlines()]
    lines = [ln for ln in lines if ln and len(ln) > 1]
    if not lines:
        return ""

    skip_prefixes = (
        "Course code:",
        "Course title:",
        "ECTS credits:",
        "Õppeaine kood:",
        "Õppeaine nimetus",
        "Õpetamise semester",
        "Õppejõud",
    )
    kept = [ln for ln in lines if not ln.startswith(skip_prefixes)]
    if not kept:
        kept = lines

    keywords = (
        "Course objectives",
        "Learning outcomes",
        "Description",
        "Content",
        "Õppeaine eesmärk",
        "Õpiväljund",
        "Aine sisu",
        "Õppemeetod",
    )
    start_idx = 0
    for i, ln in enumerate(kept):
        if any(k.lower() in ln.lower() for k in keywords):
            start_idx = i
            break
    body = kept[start_idx : start_idx + 45]
    return _clip(" ".join(body), 2500)


def _extract_course_codes(text: str) -> list[str]:
    raw = " ".join((text or "").upper().split())
    found: list[str] = []
    seen: set[str] = set()
    for pattern in COURSE_CODE_PATTERNS:
        for match in re.findall(pattern, raw):
            code = match.strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            found.append(code)
    return found


def _extract_prerequisites(raw_text: str) -> dict:
    lines = [ln.strip() for ln in (raw_text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return {"codes": [], "text": ""}

    header_idx = -1
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "prerequisite(s)" in low or "eeldus" in low:
            header_idx = i
            break
    if header_idx == -1:
        return {"codes": [], "text": ""}

    stop_markers = (
        "work load:",
        "koormus:",
        "concurrent",
        "requirements for completion",
        "assessment methods",
        "hindamismeetod",
        "iseseisev too",
        "iseseisev töö",
        "study programmes that contain the course",
        "opekavad, kus aine sisaldub",
        "õppekavad, kus aine sisaldub",
        "confirmed:",
        "kinnitatud:",
    )
    section_lines = []
    for ln in lines[header_idx + 1 :]:
        lowered = ln.lower()
        if any(marker in lowered for marker in stop_markers):
            break
        normalized = re.split(
            r"(?i)\b(?:work load|koormus|concurrent|requirements for completion|assessment methods|confirmed|kinnitatud):",
            ln,
            maxsplit=1,
        )[0].strip()
        if not normalized:
            continue
        section_lines.append(normalized)

    section_text = " ".join(section_lines).strip()
    codes = _extract_course_codes(section_text)
    return {"codes": codes, "text": _clip(section_text, 260)}


def _normalize_completed_codes(value) -> set[str]:
    out: set[str] = set()
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                code = str(row.get("code", "")).strip().upper()
                if code:
                    out.add(code)
            elif isinstance(row, str):
                out.update(_extract_course_codes(row))
        return out
    if isinstance(value, str):
        out.update(_extract_course_codes(value))
    return out


def _filter_candidates_by_prerequisites(candidates: list[dict], completed_codes: set[str]) -> tuple[list[dict], list[dict]]:
    allowed = []
    blocked = []
    for c in candidates:
        code = str(c.get("code", "")).strip().upper()
        prereq = [p for p in (c.get("prerequisites") or []) if p and p != code]
        if not prereq:
            allowed.append(c)
            continue
        missing = [p for p in prereq if p not in completed_codes]
        if missing:
            blocked.append(
                {
                    "code": code,
                    "name": str(c.get("name", "")).strip(),
                    "missing": missing[:8],
                    "prerequisites": prereq[:12],
                    "prerequisite_text": _clip(str(c.get("prerequisite_text", "")).strip(), 180),
                }
            )
            continue
        allowed.append(c)
    return allowed, blocked


def _fetch_subject_id(code: str) -> int | None:
    key = (code or "").strip().upper()
    if not key:
        return None
    if key in SUBJECT_ID_CACHE:
        return SUBJECT_ID_CACHE[key]
    try:
        url = f"{API_BASE}/public/searchSubjects?command={urllib.parse.quote(key)}"
        data = _http_get_json(url, headers=API_HDRS, timeout=20)
        if not isinstance(data, list):
            SUBJECT_ID_CACHE[key] = None
            return None
        exact = next((x for x in data if str(x.get("code", "")).upper() == key), None)
        if not exact and data:
            exact = data[0]
        sid = int(exact["id"]) if exact and exact.get("id") is not None else None
        SUBJECT_ID_CACHE[key] = sid
        return sid
    except Exception:
        SUBJECT_ID_CACHE[key] = None
        return None


def _fetch_ois_course_text(code: str) -> str:
    key = (code or "").strip().upper()
    if not key:
        return ""
    try:
        page_url = f"{OIS_SUBJECT_BASE}/{urllib.parse.quote(key)}"
        html = _http_get_text(page_url, timeout=20)
    except Exception:
        return ""

    token_en = re.search(r"OIS3_PRINTRTFEN[0-9A-F]+", html)
    token_et = re.search(r"OIS3_PRINTRTF[0-9A-F]+", html)
    token = token_en.group(0) if token_en else (token_et.group(0) if token_et else "")
    if not token:
        return ""

    try:
        rtf = _http_get_text(f"{OIS_SUBJECT_BASE}/{token}", timeout=20)
    except Exception:
        return ""
    return _rtf_to_text(rtf)


def _get_course_profile(code: str) -> dict:
    key = (code or "").strip().upper()
    if not key:
        return {
            "code": "",
            "name": "",
            "name_en": "",
            "description": "",
            "eap": None,
            "prerequisites": [],
            "prerequisite_text": "",
        }
    if key in COURSE_PROFILE_CACHE:
        return COURSE_PROFILE_CACHE[key]

    profile = {
        "code": key,
        "name": "",
        "name_en": "",
        "description": "",
        "eap": None,
        "prerequisites": [],
        "prerequisite_text": "",
    }
    subject_id = _fetch_subject_id(key)
    if subject_id is not None:
        try:
            subject = _http_get_json(f"{API_BASE}/public/subject?subjectId={subject_id}", headers=API_HDRS, timeout=20)
            if isinstance(subject, dict):
                profile["name"] = str(subject.get("name", "")).strip()
                profile["name_en"] = str(subject.get("nameEn", "")).strip()
                sph = subject.get("subjectPartHours") or {}
                if isinstance(sph, dict):
                    profile["eap"] = sph.get("eap")
        except Exception:
            pass

    ois_text = _fetch_ois_course_text(key)
    profile["description"] = _extract_course_description(ois_text)
    prereq = _extract_prerequisites(ois_text)
    profile["prerequisites"] = prereq.get("codes", [])
    profile["prerequisite_text"] = prereq.get("text", "")
    COURSE_PROFILE_CACHE[key] = profile
    return profile


def _enrich_candidates_with_profiles(candidates: list[dict], max_fetch: int = 100) -> list[dict]:
    ranked = sorted(
        candidates,
        key=lambda c: (bool(c.get("free")), int(c.get("timePreferenceScore", 0) or 0)),
        reverse=True,
    )
    for c in ranked[:max_fetch]:
        profile = _get_course_profile(str(c.get("code", "")))
        if not c.get("name"):
            c["name"] = profile.get("name", "")
        if not c.get("name_en"):
            c["name_en"] = profile.get("name_en", "")
        c["description"] = profile.get("description", "")
        c["eap"] = profile.get("eap")
        c["prerequisites"] = profile.get("prerequisites", [])
        c["prerequisite_text"] = profile.get("prerequisite_text", "")
    return candidates


def _heuristic_recommend(
    interests: str,
    group_code: str,
    candidates: list[dict],
    mandatory_courses: list[dict],
    time_preferences: list[dict],
) -> dict:
    tokens = _tokenize(interests)
    wants_online = any(t in {"online", "kaug", "kaugoppe", "remote", "distants"} for t in tokens)
    group = (group_code or "").upper()
    if group.startswith("IAI") or group.startswith("IAB"):
        track_label = "informaatika/IT suunaga"
    elif group.startswith("IC"):
        track_label = "insener-tehnilise suunaga"
    else:
        track_label = "sinu õppesuunaga"
    by_code = {str(c.get("code", "")).upper(): c for c in candidates}

    scored = []
    for c in candidates:
        code = str(c.get("code", "")).upper()
        name_blob = " ".join(
            [
                code,
                str(c.get("name", "")),
                str(c.get("name_en", "")),
                " ".join(c.get("teachers", []) or []),
            ]
        ).lower()
        desc_blob = " ".join(
            [
                str(c.get("description", "")),
                str(c.get("description_hint", "")),
                " ".join(c.get("timePreferenceNotes", []) or []),
            ]
        ).lower()

        match_tokens = [t for t in tokens if t in name_blob]
        desc_tokens = [t for t in tokens if t in desc_blob]
        score = 0
        score += 24 if c.get("free") else 6
        kind = c.get("courseKind")
        if kind == "oma":
            score += 12
        elif kind == "teine":
            score += 6
        score += min(5, len(match_tokens)) * 11
        score += min(6, len(desc_tokens)) * 13
        if wants_online and c.get("isOnline"):
            score += 6
        pref_score = max(0, min(100, int(c.get("timePreferenceScore", 0) or 0)))
        score += int(pref_score / 8)
        if time_preferences and pref_score >= 90:
            score += 8

        reason_parts = []
        if match_tokens or desc_tokens:
            tags = []
            for tok in (match_tokens + desc_tokens):
                if tok not in tags:
                    tags.append(tok)
            tags = tags[:4]
            reason_parts.append(f"kuna mainisid huvi ({', '.join(tags)}), siis aine sisu kattub sellega")
        if desc_tokens:
            reason_parts.append("ainekirjelduses on need teemad selgelt kirjeldatud")
        if c.get("free"):
            reason_parts.append("praeguse tunniplaaniga sobib konfliktita")
        if kind == "oma":
            reason_parts.append(f"sobib hästi {track_label} tudengi profiiliga")
        elif kind == "teine":
            reason_parts.append("annab väärtusliku rist-erialase oskuse")
        if c.get("isOnline"):
            reason_parts.append("sisaldab online-õppe komponenti")
        if time_preferences:
            reason_parts.append(f"ajasoovide sobivus: {pref_score}%")
        if not reason_parts:
            reason_parts.append("sobib baasvalikuks sinu huvide ja plaani kontekstis")

        scored.append(
            {
                "code": code,
                "fit_score": max(0, min(100, score)),
                "reason": _clip("; ".join(reason_parts), 240),
                "future_value": "high" if kind in {"oma", "teine"} else "medium",
                "timetable_note": "konfliktivaba" if c.get("free") else "vajab plaani muutust",
            }
        )

    scored.sort(key=lambda x: x["fit_score"], reverse=True)
    free_first = [x for x in scored if x["timetable_note"] == "konfliktivaba"]
    rest = [x for x in scored if x["timetable_note"] != "konfliktivaba"]
    recommended = (free_first + rest)[:5]

    recommended_codes = {x["code"] for x in recommended}
    future_pool = [x for x in scored if x["code"] not in recommended_codes and x["future_value"] in {"high", "medium"}]
    if len(future_pool) < 5:
        future_pool.extend([x for x in scored if x["code"] not in recommended_codes and x not in future_pool])
    future = future_pool[:5]
    mandatory_codes = [m.get("code", "") for m in mandatory_courses if m.get("code")]

    swap_suggestions = []
    if tokens and mandatory_courses and recommended:
        weak_candidates = [m for m in mandatory_courses if not any(t in str(m.get("name", "")).lower() for t in tokens)]
        replace_pick = weak_candidates[0] if weak_candidates else mandatory_courses[0]
        for rec in recommended[:3]:
            target = by_code.get(rec.get("code", ""))
            if not target:
                continue
            swap_suggestions.append(
                {
                    "replace_code": str(replace_pick.get("code", "")).upper(),
                    "replace_name": str(replace_pick.get("name", "")).strip(),
                    "with_code": rec.get("code", ""),
                    "with_name": str(target.get("name", "")).strip(),
                    "reason": _clip(
                        f"Kui asendad aine {replace_pick.get('code', '')} ainega {rec.get('code', '')}, "
                        f"saad rohkem {', '.join(tokens[:3])} fookust ja see võib aidata tulevase praktika/töö suunas.",
                        260,
                    ),
                }
            )

    summary = (
        f"Heuristiline soovitus {len(candidates)} kandidaadist. "
        f"Eelistasin 5 parimat ainet, mis sobivad huvide, õppesuuna ja ajasoovidega."
    )
    if time_preferences:
        summary += f" Arvestasin {len(time_preferences)} päeva/aja eelistust."
    if mandatory_codes:
        summary += f" Kohustuslike ainete kontekst: {', '.join(mandatory_codes[:6])}."

    return {
        "mode": "heuristic",
        "summary": summary,
        "recommended_courses": recommended,
        "future_courses": future,
        "swap_suggestions": swap_suggestions[:3],
    }


def _openai_recommend(
    api_key: str,
    interests: str,
    group_code: str,
    semester: int,
    mandatory_courses: list[dict],
    candidates: list[dict],
    time_preferences: list[dict],
) -> dict:
    system_prompt = (
        "You are an academic planning assistant for TalTech students. "
        "Given student interests and filtered timetable-compatible courses, "
        "optimize for the student's requested free/filled day-time windows. "
        "Give practical, student-style reasoning tied to interests, study track fit, and future internship/work value. "
        "Return concise recommendation JSON only."
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
                "description": _clip(str(c.get("description", "")), 1200),
                "prerequisites": c.get("prerequisites") or [],
                "prerequisite_text": _clip(str(c.get("prerequisite_text", "")), 180),
                "timePreferenceScore": int(c.get("timePreferenceScore", 0) or 0),
                "timePreferenceNotes": c.get("timePreferenceNotes") or [],
            }
        )

    user_payload = {
        "student_context": {"group_code": group_code, "semester": semester, "interests": interests},
        "time_preferences": time_preferences,
        "mandatory_courses": mandatory_courses[:20],
        "candidate_courses": compact_candidates,
        "selection_rule": "recommended_courses should be top 5 best fitting now. future_courses should suggest useful courses for later semester planning. Include swap_suggestions for replacing one current course with a better interest-aligned one when possible.",
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
            "swap_suggestions": [
                {
                    "replace_code": "string",
                    "replace_name": "string",
                    "with_code": "string",
                    "with_name": "string",
                    "reason": "string",
                }
            ],
        },
    }

    payload = {
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    if AZURE_OPENAI_ENDPOINT:
        deployment = AZURE_OPENAI_DEPLOYMENT or OPENAI_MODEL
        if not deployment:
            raise ValueError("Azure OpenAI deployment name missing")
        azure_headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        version_candidates = []
        for v in [
            AZURE_OPENAI_API_VERSION,
            "2025-01-01-preview",
            "2024-10-21",
            "2024-08-01-preview",
            "2023-12-01-preview",
            "2023-03-15-preview",
        ]:
            vv = str(v or "").strip()
            if vv and vv not in version_candidates:
                version_candidates.append(vv)

        response_data = None
        dep_errors = []
        for api_ver in version_candidates:
            azure_dep_url = (
                f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/"
                f"{urllib.parse.quote(deployment)}/chat/completions"
                f"?api-version={urllib.parse.quote(api_ver)}"
            )
            try:
                response_data = _http_post_json(
                    azure_dep_url,
                    payload,
                    headers=azure_headers,
                    timeout=40,
                )
                break
            except Exception as dep_exc:
                dep_errors.append(f"{api_ver}: {dep_exc}")

        if response_data is None:
            azure_v1_url = f"{AZURE_OPENAI_ENDPOINT}/openai/v1/chat/completions"
            payload_v1 = dict(payload)
            payload_v1["model"] = deployment
            try:
                response_data = _http_post_json(
                    azure_v1_url,
                    payload_v1,
                    headers=azure_headers,
                    timeout=40,
                )
            except Exception as v1_exc:
                raise ValueError(
                    "Azure OpenAI call failed. "
                    f"Deployment route errors: {' | '.join(dep_errors[:4])}. "
                    f"V1 route error: {v1_exc}. "
                    "Check AZURE_OPENAI_DEPLOYMENT and AZURE_OPENAI_API_VERSION."
                )
    else:
        payload["model"] = OPENAI_MODEL
        response_data = _http_post_json(
            OPENAI_API,
            payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=40,
        )

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

    def normalize_swaps(rows: list[dict]) -> list[dict]:
        out = []
        for row in rows or []:
            replace_code = str(row.get("replace_code", "")).strip().upper()
            with_code = str(row.get("with_code", "")).strip().upper()
            if not with_code or with_code not in candidate_codes:
                continue
            out.append(
                {
                    "replace_code": replace_code,
                    "replace_name": _clip(str(row.get("replace_name", "")), 120),
                    "with_code": with_code,
                    "with_name": _clip(str(row.get("with_name", "")), 120),
                    "reason": _clip(str(row.get("reason", "")), 260),
                }
            )
        return out

    swaps = normalize_swaps(parsed.get("swap_suggestions", []))

    if not recommended:
        raise ValueError("LLM returned no usable recommendations")
    if not future:
        future = [r for r in recommended if r.get("future_value") in {"high", "medium"}][:5]

    return {
        "mode": "openai",
        "summary": _clip(str(parsed.get("summary", "")).strip(), 500),
        "recommended_courses": recommended[:5],
        "future_courses": future[:5],
        "swap_suggestions": swaps[:3],
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
        time_preferences = req.get("time_preferences") or []
        completed_courses = req.get("completed_courses")
        known_course_codes = req.get("known_course_codes")
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
                    "description_hint": str(c.get("descriptionHint", "")).strip(),
                    "timePreferenceScore": int(c.get("timePreferenceScore", 0) or 0),
                    "timePreferenceNotes": c.get("timePreferenceNotes") or [],
                }
            )

        if not normalized_candidates:
            self._send_json(400, {"error": "No usable candidates"})
            return

        normalized_time_preferences = []
        for pref in time_preferences[:20]:
            if not isinstance(pref, dict):
                continue
            mode = str(pref.get("mode", "")).strip().lower()
            day = int(pref.get("day", 0) or 0)
            start = str(pref.get("start", "")).strip()
            end = str(pref.get("end", "")).strip()
            if mode not in {"free", "filled"}:
                continue
            if day < 1 or day > 5:
                continue
            if not start or not end:
                continue
            normalized_time_preferences.append({"mode": mode, "day": day, "start": start, "end": end})

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

        normalized_candidates = _enrich_candidates_with_profiles(normalized_candidates, max_fetch=100)
        completed_codes = _normalize_completed_codes(completed_courses)
        completed_codes.update(_normalize_completed_codes(known_course_codes))
        normalized_candidates, blocked_by_prereq = _filter_candidates_by_prerequisites(
            normalized_candidates,
            completed_codes,
        )

        if not normalized_candidates:
            self._send_json(
                200,
                {
                    "mode": "heuristic",
                    "summary": "Kõik kandidaadid langesid välja eeldusainete kontrolli tõttu.",
                    "recommended_courses": [],
                    "future_courses": [],
                    "swap_suggestions": [],
                    "blocked_due_prerequisites": blocked_by_prereq[:40],
                },
            )
            return

        azure_api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip() or os.getenv("AZURE_API_KEY", "").strip()
        api_key = azure_api_key or os.getenv("OPENAI_API_KEY", "").strip()
        if api_key:
            try:
                result = _openai_recommend(
                    api_key=api_key,
                    interests=interests,
                    group_code=group_code,
                    semester=semester,
                    mandatory_courses=normalized_mandatory,
                    candidates=normalized_candidates,
                    time_preferences=normalized_time_preferences,
                )
                if blocked_by_prereq:
                    result["blocked_due_prerequisites"] = blocked_by_prereq[:40]
                    result["summary"] = _clip(
                        f"{result.get('summary', '')} "
                        f"Eeldusainete puudumise tõttu peitsin {len(blocked_by_prereq)} ainet.",
                        520,
                    )
                self._send_json(200, result)
                return
            except Exception as exc:
                fallback = _heuristic_recommend(
                    interests,
                    group_code,
                    normalized_candidates,
                    normalized_mandatory,
                    normalized_time_preferences,
                )
                fallback["warning"] = _clip(
                    f"OpenAI call failed, fallback used: {type(exc).__name__}: {exc}",
                    700,
                )
                if blocked_by_prereq:
                    fallback["blocked_due_prerequisites"] = blocked_by_prereq[:40]
                    fallback["summary"] = _clip(
                        f"{fallback.get('summary', '')} "
                        f"Eeldusainete puudumise tõttu peitsin {len(blocked_by_prereq)} ainet.",
                        520,
                    )
                self._send_json(200, fallback)
            return

        fallback = _heuristic_recommend(
            interests,
            group_code,
            normalized_candidates,
            normalized_mandatory,
            normalized_time_preferences,
        )
        fallback["warning"] = (
            "OpenAI pole seadistatud. Sea AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT "
            "+ AZURE_OPENAI_DEPLOYMENT (.env.local või shell env), et kasutada päris LLM-i."
        )
        if blocked_by_prereq:
            fallback["blocked_due_prerequisites"] = blocked_by_prereq[:40]
            fallback["summary"] = _clip(
                f"{fallback.get('summary', '')} "
                f"Eeldusainete puudumise tõttu peitsin {len(blocked_by_prereq)} ainet.",
                520,
            )
        self._send_json(200, fallback)

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

    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
