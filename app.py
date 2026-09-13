"""
The one server this whole project needs. It receives a username+password
in a single request, uses them once to talk to Moodle, and returns the
attendance data — nothing is ever written to disk, logged, or kept in
memory past the end of the request.

Deploy this on Render's free tier (or any host that runs a Python web
service). Nothing here is IIMK-specific in a way that needs secrets —
the only "config" is course_map.json sitting next to this file, which
Naitik fills in once and is the same for every batchmate.
"""
import os
import re
import csv
import json
import io
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from bs4 import BeautifulSoup

BASE_URL = "https://uglms.iimk.ac.in"

# Naitik's shared Sem-III schedule sheet — public, view-only, no login needed.
SCHEDULE_SHEET_ID = "1wbVXe5Ivk5UIS8anrEgn-V3LHQKDkLy3uHfqpe7mN-I"
SCHEDULE_GID = "278743392"

# Maps the short codes used in the schedule sheet (e.g. "OB-7") to full
# subject names. Extend this if new subjects/codes show up.
SUBJECT_CODES = {
    "WIH": "World and Indian History",
    "OB": "Organisational Behaviour",
    "ATAP": "Algorithmic Thinking and Programming",
    "MO": "Mathematical Optimization",
    "ITL": "Introduction to Law",
    "CP": "Community Project",
}

LANGUAGE_COLUMN = {
    "japanese": "FLC (J)",
    "german": "FLC (G)",
    "french": "FLC (F)",
    "spanish": "FLC (S)",
}

app = Flask(__name__)
# Only allow requests from the actual frontend's origin — replace this
# with your real GitHub Pages URL once you know it.
CORS(app, origins=["https://naitikagg2311-tech.github.io"])

# Per-IP throttling. /api/attendance forwards credentials to Moodle, so
# without a limit here this endpoint could be scripted into a
# brute-force/credential-stuffing proxy against IIMK's login system.
limiter = Limiter(get_remote_address, app=app, default_limits=[])


def moodle_login(username, password, http):
    """Standard Moodle form login against a shared session. Returns True/False."""
    http.headers.update({"User-Agent": "Mozilla/5.0 (attendance-tool)"})
    login_page = http.get(f"{BASE_URL}/login/index.php", timeout=15)
    match = re.search(r'name="logintoken" value="([^"]+)"', login_page.text)
    logintoken = match.group(1) if match else ""
    resp = http.post(
        f"{BASE_URL}/login/index.php",
        data={"username": username, "password": password, "logintoken": logintoken},
        timeout=15,
    )
    if "loginerrors" in resp.text or 'id="login"' in resp.text:
        return False
    return True


def get_token(username, password, http):
    """Exchanges credentials for a webservice token. Returns None on failure."""
    resp = http.post(
        f"{BASE_URL}/login/token.php",
        data={"username": username, "password": password, "service": "moodle_mobile_app"},
        timeout=15,
    )
    data = resp.json()
    return data.get("token")


def ws_call(http, token, function, **params):
    resp = http.get(
        f"{BASE_URL}/webservice/rest/server.php",
        params={"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params},
        timeout=15,
    )
    return resp.json()


TIME_RANGE_RE = re.compile(r"(\d{1,2}(:\d{2})?\s*[AP]M\s*-\s*\d{1,2}(:\d{2})?\s*[AP]M)", re.IGNORECASE)


def parse_attendance_table(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="generaltable")
    if not table:
        return []
    body = table.find("tbody")
    if not body:
        return []
    records = []
    for row in body.find_all("tr"):
        date_cell = row.find("td", class_=lambda c: c and "datecol" in c)
        desc_cell = row.find("td", class_=lambda c: c and "desccol" in c)
        status_cell = row.find("td", class_=lambda c: c and "statuscol" in c)
        points_cell = row.find("td", class_=lambda c: c and "pointscol" in c)
        if date_cell is None or status_cell is None:
            continue
        # The date cell's date + time may render on one line or two
        # depending on Moodle's view mode — pull the time out with a
        # regex instead of assuming a fixed line count.
        full_text = " ".join(date_cell.stripped_strings)
        time_match = TIME_RANGE_RE.search(full_text)
        time_val = time_match.group(1) if time_match else ""
        date_val = full_text.replace(time_val, "").strip(" ,")
        records.append({
            "date": date_val,
            "time": time_val,
            "session": desc_cell.get_text(strip=True) if desc_cell else "",
            "status": status_cell.get_text(strip=True),
            "points": points_cell.get_text(strip=True) if points_cell else "",
        })
    return records


def _normalize(name):
    """Lowercase, collapse whitespace/dashes/punctuation, and drop the
    section/batch letter entirely — 'Section A' vs 'Section B' vs
    'Batch A' must never be the reason two names fail to match, since
    the same shared site serves students in different sections."""
    name = name.lower()
    name = re.sub(r"[-–—_,./]", " ", name)
    name = re.sub(r"\b(section|sec|batch)\s+[a-z]\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def load_course_map():
    """Returns list of (normalized_name, entry) — matched by substring, not exact equality."""
    path = os.path.join(os.path.dirname(__file__), "course_map.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        entries = json.load(f)
    return [(_normalize(e["course_name"]), e) for e in entries]


def match_course(course_map, fullname):
    """Finds the best course_map entry whose name is contained in (or
    contains) the real Moodle fullname, after normalizing both sides."""
    norm = _normalize(fullname)
    for mapped_norm, entry in course_map:
        if mapped_norm in norm or norm in mapped_norm:
            return entry
    return {}


@app.route("/api/attendance", methods=["POST"])
@limiter.limit("5 per minute")
def get_attendance():
    body = request.get_json(silent=True) or {}
    username = body.get("username")
    password = body.get("password")
    if not username or not password:
        return jsonify({"error": "Username and password required."}), 400

    # Two separate authenticated clients are needed — cookie-session login
    # for scraping attendance pages (mod_attendance has no webservice
    # function), and a token for the clean JSON API (course listing).
    # They're independent, so run them at the same time instead of one
    # after another — this alone cuts a few seconds off every login.
    token_http = requests.Session()
    cookie_http = requests.Session()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            token_future = pool.submit(get_token, username, password, token_http)
            login_future = pool.submit(moodle_login, username, password, cookie_http)
            token = token_future.result()
            logged_in = login_future.result()

        if not token or not logged_in:
            return jsonify({"error": "Login failed. Check your username and password."}), 401

        # From here on, `username`/`password` are never referenced again —
        # only `token` and `cookie_http` (already-authenticated) are used.

        course_map = load_course_map()
        site_info = ws_call(token_http, token, "core_webservice_get_site_info")
        if "userid" not in site_info:
            return jsonify({"error": "Moodle didn't return a valid session. Try again."}), 502
        courses = ws_call(token_http, token, "core_enrol_get_users_courses", userid=site_info["userid"])
    except requests.exceptions.RequestException:
        return jsonify({"error": "Couldn't reach the LMS right now. Try again shortly."}), 502

    def fetch_one_course(course):
        """Everything needed for one course, run in its own thread."""
        contents = ws_call(token_http, token, "core_course_get_contents", courseid=course["id"])
        mapped = match_course(course_map, course.get("fullname", ""))
        entries = []
        for section in contents:
            for module in section.get("modules", []):
                if module.get("modname") != "attendance":
                    continue
                resp = cookie_http.get(f"{BASE_URL}/mod/attendance/view.php",
                                        params={"id": module["id"], "view": 5}, timeout=15)
                entries.append({
                    "course_id": course["id"],
                    "course_name": course.get("fullname", ""),
                    "display_name": mapped.get("display_name", course.get("fullname", "")),
                    "semester": mapped.get("semester"),
                    "total_hours": mapped.get("total_hours"),
                    "sessions": parse_attendance_table(resp.text),
                })
        return entries

    result = []
    # requests.Session isn't guaranteed thread-safe for concurrent requests
    # sharing one connection pool under heavy load, but Moodle's session
    # cookie auth only needs the same cookies sent — a modest pool size
    # here trades a little safety margin for a large real-world speedup.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch_one_course, c) for c in courses]
        for future in as_completed(futures):
            result.extend(future.result())

    return jsonify({"courses": result})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


def parse_code(raw):
    """
    'MO-25 (AR)' -> ('MO', 'Mathematical Optimization', '25 (AR)', True)
    'CP 11'      -> ('CP', 'Community Project', '11', True)
    'DEEPAWALI'  -> ('DEEPAWALI', 'DEEPAWALI', '', False)  — not a real subject
    Checks against known codes explicitly (handles space, hyphen, or no
    separator at all) rather than assuming one fixed format — the sheet
    isn't consistent about which separator it uses per subject.
    Anything that isn't a known code (holidays, exam periods, admin
    sessions like POSH/Anti-Ragging, 'Buffer/Quiz') is returned as-is,
    flagged as not a real academic subject.
    """
    raw = raw.strip()
    upper = raw.upper()
    for code, name in SUBJECT_CODES.items():
        if upper == code or upper.startswith(code + " ") or upper.startswith(code + "-"):
            rest = raw[len(code):].strip(" -")
            return code, name, rest, True
    return raw, raw, "", False


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_date_nice(date_str):
    """'Friday, 4 September, 2026' -> '4th Sept'"""
    try:
        dt = datetime.strptime(date_str, "%A, %d %B, %Y")
        return f"{ordinal(dt.day)} {dt.strftime('%b')}"
    except ValueError:
        return date_str


def format_time_range(raw_time):
    """
    '14:30-15:30' -> '2:30-3:30 pm'. The sheet's own hours are ambiguous
    below 10 (e.g. '01:30' really means 1:30 PM) because every class
    slot in this schedule falls between 10 AM and 6 PM — so any hour
    written below 10 is normalized up by 12 before formatting.
    """
    try:
        start_raw, end_raw = raw_time.split("-")

        def norm(t):
            h, m = map(int, t.strip().split(":"))
            if h < 10:
                h += 12
            return h, m

        def to12(h):
            period = "am" if h < 12 else "pm"
            h12 = h % 12 or 12
            return h12, period

        sh, sm = norm(start_raw)
        eh, em = norm(end_raw)
        sh12, speriod = to12(sh)
        eh12, eperiod = to12(eh)
        if speriod == eperiod:
            return f"{sh12}:{sm:02d}-{eh12}:{em:02d} {eperiod}"
        return f"{sh12}:{sm:02d} {speriod}-{eh12}:{em:02d} {eperiod}"
    except Exception:
        return raw_time


def fetch_schedule_sheet():
    """Downloads the full sheet as CSV — every row, no virtualization limits."""
    url = f"https://docs.google.com/spreadsheets/d/{SCHEDULE_SHEET_ID}/export"
    resp = requests.get(url, params={"format": "csv", "gid": SCHEDULE_GID}, timeout=15)
    resp.raise_for_status()
    return list(csv.reader(io.StringIO(resp.text)))


def build_personal_schedule(section, language):
    """
    section: "A" or "B"
    language: one of "japanese", "german", "french", "spanish"
    Returns every session for that student, in order, each tagged with
    its subject and a parsed datetime for sorting/filtering.
    """
    rows = fetch_schedule_sheet()

    header_idx = None
    for i, row in enumerate(rows):
        if len(row) >= 3 and row[0].strip() == "Date" and row[1].strip() == "Day":
            header_idx = i
            break
    if header_idx is None:
        return []

    header = rows[header_idx]
    col_index = {name.strip(): i for i, name in enumerate(header) if name.strip()}

    section_col = col_index.get(f"Sec {section.upper()}")
    language_col = col_index.get(LANGUAGE_COLUMN.get(language.lower(), ""))
    time_col, date_col, day_col = col_index.get("Time"), col_index.get("Date"), col_index.get("Day")

    sessions = []
    for row in rows[header_idx + 1:]:
        if not row or date_col >= len(row) or not row[date_col].strip():
            continue
        date_str, day_str = row[date_col].strip(), row[day_col].strip()
        time_str = row[time_col].strip() if time_col < len(row) else ""

        for col in filter(None, [section_col, language_col]):
            if col >= len(row):
                continue
            raw = row[col].strip()
            if not raw or raw.upper() in ("LUNCH BREAK", "BLOCKED"):
                continue
            if col == language_col:
                # We already know which language this is from context —
                # no need to re-parse the "FLC (F)-11" style code.
                prefix, session_no, is_subject = "FLC", raw.split("-")[-1].strip(), True
                name = f"Foreign Language ({language.capitalize()})"
            else:
                prefix, name, session_no, is_subject = parse_code(raw)
            try:
                start_time = time_str.split("-")[0].strip()
                dt = datetime.strptime(f"{date_str} {start_time}", "%A, %d %B, %Y %H:%M")
            except ValueError:
                dt = None
            sessions.append({
                "date": date_str,
                "date_nice": format_date_nice(date_str),
                "day": day_str,
                "time": time_str,
                "time_nice": format_time_range(time_str),
                "code": raw,
                "subject": name,
                "session_no": session_no,
                "is_subject": is_subject,
                "datetime": dt.isoformat() if dt else None,
            })

    sessions.sort(key=lambda s: s["datetime"] or "")
    return sessions


@app.route("/api/schedule")
@limiter.limit("30 per minute")
def get_schedule():
    section = request.args.get("section", "A")
    language = request.args.get("language", "")
    if language.lower() not in LANGUAGE_COLUMN:
        return jsonify({"error": "language must be one of: japanese, german, french, spanish"}), 400

    sessions = build_personal_schedule(section, language)
    now = datetime.now().isoformat()

    upcoming = [s for s in sessions if s["datetime"] and s["datetime"] >= now]
    next_class = next((s for s in upcoming if s["is_subject"]), None)

    remaining_by_subject = {}
    for s in upcoming:
        if not s["is_subject"]:
            continue
        remaining_by_subject[s["subject"]] = remaining_by_subject.get(s["subject"], 0) + 1

    return jsonify({
        "next_class": next_class,
        "remaining_by_subject": remaining_by_subject,
        "all_sessions": sessions,
    })


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
