# UG LMS Attendance Register

Two pieces:
- `backend/` — the small server that logs into Moodle on a batchmate's
  behalf, once per request, and never stores anything. Needs to run
  somewhere with real compute (Render's free tier works).
- `frontend/` — the actual website (`index.html`), plain HTML/CSS/JS,
  no build step. Goes on GitHub Pages.

## The two things only you can do (no way around these)

Nobody — no AI, no script — can put files onto *your* GitHub account or
create *your* hosting account without you clicking through it yourself.
Here's the shortest possible version of each:

### 1. Put the frontend on GitHub Pages
1. Go to github.com, log in, click **New repository**. Name it anything,
   e.g. `attendance-site`. Keep it Public.
2. On the new repo's page, click **Add file → Upload files**, drag in
   `frontend/index.html`, commit.
3. Go to the repo's **Settings → Pages**, set the source branch to
   `main` and folder to `/ (root)`, save.
4. GitHub gives you a URL like `https://yourname.github.io/attendance-site/`
   — that's the live site.

### 2. Put the backend on Render
1. Go to render.com, sign up (free), click **New → Web Service**.
2. Choose "Deploy from a public Git repo" — so you'll want the
   `backend/` folder in a GitHub repo too (can be the same repo as the
   frontend, or a separate one).
3. Render auto-detects `requirements.txt`. Set the start command to:
   `gunicorn app:app`
4. Once deployed, Render gives you a URL like
   `https://your-app.onrender.com` — that's your `API_BASE`.

### After both are live
- Open `frontend/index.html`, edit the `API_BASE` constant near the top
  of the `<script>` to your real Render URL, re-upload it to GitHub.
- Open `backend/app.py`, edit the `CORS(app, origins=[...])` line to
  your real GitHub Pages URL, redeploy.
- Fill in `backend/course_map.json` with your real course IDs and
  semesters (run `discover_courses.py` locally once to get the IDs —
  this is the one script that's still worth running, since there's no
  other way to see your course list without logging in somewhere).

Render's free tier sleeps after inactivity — the first request after
a quiet period takes ~30 seconds to wake up. That's normal.
