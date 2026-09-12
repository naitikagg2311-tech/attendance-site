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
import json
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup

BASE_URL = "https://uglms.iimk.ac.in"

app = Flask(__name__)
# Only allow requests from the actual frontend's origin — replace this
# with your real GitHub Pages URL once you know it.
CORS(app, origins=["https://YOUR-GITHUB-USERNAME.github.io"])


def moodle_login(username, password):
    """Standard Moodle form login. Returns an authenticated session, or None."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (attendance-tool)"})
    login_page = session.get(f"{BASE_URL}/login/index.php", timeout=15)
    match = re.search(r'name="logintoken" value="([^"]+)"', login_page.text)
    logintoken = match.group(1) if match else ""
    resp = session.post(
        f"{BASE_URL}/login/index.php",
        data={"username": username, "password": password, "logintoken": logintoken},
        timeout=15,
    )
    if "loginerrors" in resp.text or 'id="login"' in resp.text:
        return None
    return session


def get_token(username, password):
    """Exchanges credentials for a webservice token. Returns None on failure."""
    resp = requests.post(
        f"{BASE_URL}/login/token.php",
        data={"username": username, "password": password, "service": "moodle_mobile_app"},
        timeout=15,
    )
    data = resp.json()
    return data.get("token")


def ws_call(token, function, **params):
    resp = requests.get(
        f"{BASE_URL}/webservice/rest/server.php",
        params={"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params},
        timeout=15,
    )
    return resp.json()


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
        date_lines = [s.strip() for s in date_cell.stripped_strings]
        records.append({
            "date": date_lines[0] if date_lines else "",
            "time": date_lines[1] if len(date_lines) > 1 else "",
            "session": desc_cell.get_text(strip=True) if desc_cell else "",
            "status": status_cell.get_text(strip=True),
            "points": points_cell.get_text(strip=True) if points_cell else "",
        })
    return records


def load_course_map():
    """course_name -> {"semester": int, "display_name": str}"""
    path = os.path.join(os.path.dirname(__file__), "course_map.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        entries = json.load(f)
    return {e["course_name"]: e for e in entries}


@app.route("/api/attendance", methods=["POST"])
def get_attendance():
    body = request.get_json(silent=True) or {}
    username = body.get("username")
    password = body.get("password")
    if not username or not password:
        return jsonify({"error": "Username and password required."}), 400

    token = get_token(username, password)
    if not token:
        return jsonify({"error": "Login failed. Check your username and password."}), 401

    session = moodle_login(username, password)
    if session is None:
        return jsonify({"error": "Login failed. Check your username and password."}), 401

    # From here on, `username`/`password` are never referenced again —
    # only `token` and `session` (already-authenticated) are used.

    course_map = load_course_map()
    site_info = ws_call(token, "core_webservice_get_site_info")
    courses = ws_call(token, "core_enrol_get_users_courses", userid=site_info["userid"])

    result = []
    for course in courses:
        contents = ws_call(token, "core_course_get_contents", courseid=course["id"])
        mapped = course_map.get(course.get("fullname", ""), {})
        for section in contents:
            for module in section.get("modules", []):
                if module.get("modname") != "attendance":
                    continue
                resp = session.get(f"{BASE_URL}/mod/attendance/view.php",
                                    params={"id": module["id"]}, timeout=15)
                result.append({
                    "course_id": course["id"],
                    "course_name": course.get("fullname", ""),
                    "display_name": mapped.get("display_name", course.get("fullname", "")),
                    "semester": mapped.get("semester"),
                    "sessions": parse_attendance_table(resp.text),
                })

    return jsonify({"courses": result})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
