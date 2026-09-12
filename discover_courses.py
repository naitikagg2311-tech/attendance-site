"""
Lists every course you're enrolled in and writes course_map_template.json
with a blank "semester" field next to each — fill those in by hand once,
save as course_map.json, and every other script reads from that.

Usage:
    export BMS_TOKEN="your_token"
    python3 discover_courses.py
"""
import os
import sys
import json
import requests

BASE_URL = "https://uglms.iimk.ac.in"


def ws_call(token: str, function: str, **params) -> dict:
    resp = requests.get(
        f"{BASE_URL}/webservice/rest/server.php",
        params={"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params},
        timeout=15,
    )
    return resp.json()


def main():
    token = os.environ.get("BMS_TOKEN")
    if not token:
        print("Set BMS_TOKEN env var first.", file=sys.stderr)
        sys.exit(1)

    site_info = ws_call(token, "core_webservice_get_site_info")
    courses = ws_call(token, "core_enrol_get_users_courses", userid=site_info["userid"])

    template = []
    for c in sorted(courses, key=lambda c: c.get("shortname", "")):
        template.append({
            "course_id": c["id"],
            "course_name": c.get("fullname", ""),
            "shortname": c.get("shortname", ""),
            "semester": None,          # <-- fill this in: 1, 2, 3, ...
            "display_name": c.get("fullname", ""),  # <-- edit if you want a shorter label for the UI
        })

    with open("course_map_template.json", "w") as f:
        json.dump(template, f, indent=2)

    print(f"Wrote course_map_template.json with {len(template)} courses.")
    print("Fill in 'semester' for each (and shorten 'display_name' if you like),")
    print("then save the file as course_map.json.")


if __name__ == "__main__":
    main()
