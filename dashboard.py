import sqlite3
import uuid
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, session

BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "bot.db")

# Load .env
_env = {}
_env_path = BASE_DIR / ".env"
if _env_path.exists():
    for _line in _env_path.read_text("utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _env[_k.strip()] = _v.strip()

PASSWORD = _env.get("DASHBOARD_PASS", "admin123")
SECRET_KEY = str(uuid.uuid4())

app = Flask(__name__)
app.secret_key = SECRET_KEY

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Login - Bot Dashboard</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, sans-serif; background: #0f0f0f; color: #eee; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: #1a1a1a; padding: 40px; border-radius: 12px; width: 340px; }
    h1 { font-size: 22px; margin-bottom: 20px; text-align: center; }
    input { width: 100%; padding: 12px; background: #0f0f0f; border: 1px solid #333; border-radius: 8px; color: #fff; font-size: 15px; margin-bottom: 15px; }
    button { width: 100%; padding: 12px; background: #00a86b; border: none; border-radius: 8px; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; }
    .err { color: #e74c3c; text-align: center; margin-bottom: 10px; font-size: 13px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>🔐 Dashboard Login</h1>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post">
      <input type="password" name="pass" placeholder="Password" autofocus>
      <button type="submit">Masuk</button>
    </form>
  </div>
</body>
</html>
"""

HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Bot Dashboard</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, sans-serif; background: #0f0f0f; color: #eee; padding: 20px; }
    h1 { font-size: 24px; margin-bottom: 20px; color: #fff; }
    h2 { font-size: 18px; margin: 20px 0 10px; color: #aaa; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #222; }
    th { background: #1a1a1a; color: #888; font-weight: 600; }
    tr:hover td { background: #1a1a1a; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
    .ok { background: #00a86b; color: #fff; }
    .err { background: #e74c3c; color: #fff; }
    .pen { background: #f39c12; color: #fff; }
    nav { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
    nav a { color: #888; text-decoration: none; padding: 6px 14px; border-radius: 6px; background: #1a1a1a; }
    nav a.active { color: #fff; background: #00a86b; }
    nav .logout { margin-left: auto; background: #e74c3c; color: #fff; }
    .box { background: #1a1a1a; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
    .box label { display: block; color: #888; font-size: 11px; margin-bottom: 4px; }
    .box .val { font-size: 22px; font-weight: 700; }
    .row { display: flex; gap: 15px; flex-wrap: wrap; }
    .row .box { flex: 1; min-width: 120px; }
  </style>
</head>
<body>
  <h1>🤖 Bot Dashboard</h1>
  <nav>
    <a href="/" class="{% if tab == 'users' %}active{% endif %}">Users</a>
    <a href="/?tab=fb" class="{% if tab == 'fb' %}active{% endif %}">Facebook</a>
    <a href="/?tab=posts" class="{% if tab == 'posts' %}active{% endif %}">Social Posts</a>
    <a href="/?tab=schedule" class="{% if tab == 'schedule' %}active{% endif %}">Schedule</a>
    <a href="/logout" class="logout">Logout</a>
  </nav>

  <div class="row">
    <div class="box"><label>Users</label><div class="val">{{ stats.users }}</div></div>
    <div class="box"><label>FB Linked</label><div class="val">{{ stats.fb }}</div></div>
    <div class="box"><label>Uploads</label><div class="val">{{ stats.uploads }}</div></div>
    <div class="box"><label>Scheduled</label><div class="val">{{ stats.scheduled }}</div></div>
  </div>

  {% if tab == 'users' %}
  <h2>👤 Users</h2>
  <table>
    <tr><th>ID</th><th>Sub ID</th><th>Username</th><th>Expiry</th><th>Admin</th><th>FB Linked</th></tr>
    {% for u in users %}
    <tr>
      <td>{{ u.user_id }}</td><td>{{ u.sub_id or '-' }}</td><td>{{ u.username or '-' }}</td>
      <td>{{ u.expiry or '-' }}</td><td>{{ '👑' if u.is_admin else '' }}</td>
      <td>{{ '✅' if u.fb else '' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  {% if tab == 'fb' %}
  <h2>📘 Facebook Accounts</h2>
  <table>
    <tr><th>User ID</th><th>Page Name</th><th>Page ID</th></tr>
    {% for f in fb %}
    <tr><td>{{ f.user_id }}</td><td>{{ f.page_name }}</td><td>{{ f.page_id }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}

  {% if tab == 'posts' %}
  <h2>📤 Social Uploads</h2>
  <table>
    <tr><th>ID</th><th>User</th><th>Platform</th><th>Caption</th><th>Status</th><th>Error</th><th>Date</th></tr>
    {% for p in posts %}
    <tr>
      <td>{{ p.id }}</td><td>{{ p.user_id }}</td>
      <td>{{ p.platform }}</td><td>{{ p.caption[:50] }}..</td>
      <td><span class="badge {% if p.status == 'success' %}ok{% elif p.status == 'error' %}err{% else %}pen{% endif %}">{{ p.status }}</span></td>
      <td style="color:#e74c3c;font-size:11px">{{ p.error[:40] if p.error else '' }}</td>
      <td>{{ p.created_at[:10] if p.created_at else '' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  {% if tab == 'schedule' %}
  <h2>⏰ Scheduled Posts</h2>
  <table>
    <tr><th>ID</th><th>User</th><th>Schedule</th><th>Caption</th><th>Status</th></tr>
    {% for s in schedule %}
    <tr>
      <td>{{ s.id }}</td><td>{{ s.user_id }}</td>
      <td>{{ s.schedule_time }}</td><td>{{ s.caption[:50] }}..</td>
      <td><span class="badge {% if s.status == 'sent' %}ok{% else %}pen{% endif %}">{{ s.status }}</span></td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
</body>
</html>
"""


def query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("pass") == PASSWORD:
            session["auth"] = True
            return redirect("/")
        return render_template_string(LOGIN_HTML, error="Password salah")
    return render_template_string(LOGIN_HTML, error="")


@app.route("/logout")
def logout():
    session.pop("auth", None)
    return redirect("/login")


@app.route("/")
def index():
    if not session.get("auth"):
        return redirect("/login")

    tab = request.args.get("tab", "users")

    users = query("SELECT user_id, sub_id, username, expiry, is_admin FROM users ORDER BY expiry DESC")
    fb_accounts = query("SELECT user_id, page_id, page_name FROM facebook_accounts ORDER BY created_at DESC")
    posts = query("SELECT id, user_id, platform, caption, status, error, created_at FROM social_posts ORDER BY created_at DESC LIMIT 50")
    schedule = query("SELECT id, user_id, schedule_time, caption, status FROM scheduled_posts ORDER BY schedule_time DESC LIMIT 50")

    fb_user_ids = {f["user_id"] for f in fb_accounts}
    for u in users:
        u["fb"] = u["user_id"] in fb_user_ids

    stats = {
        "users": len(users),
        "fb": len(fb_accounts),
        "uploads": len(posts),
        "scheduled": sum(1 for s in schedule if s["status"] == "pending"),
    }

    return render_template_string(HTML, tab=tab, users=users, fb=fb_accounts, posts=posts, schedule=schedule, stats=stats)


if __name__ == "__main__":
    print("Dashboard: http://localhost:5555")
    print("Password: 97560")
    app.run(host="127.0.0.1", port=5555, debug=True)
