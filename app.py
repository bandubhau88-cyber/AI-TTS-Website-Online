from flask import Flask, request, send_file, jsonify, session, redirect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import edge_tts
import asyncio
import os
import uuid
import re
import json
import sqlite3
import secrets
import urllib.parse
import urllib.request
import base64
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps

# =========================================================
# AI VOICE STUDIO PRO - ONLINE VERSION + ACCOUNTS/PLANS
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FOLDER = os.path.join(BASE_DIR, "generated_audio")
HISTORY_FILE = os.path.join(BASE_DIR, "audio_history.json")  # legacy compatibility
DB_FILE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "ai_voice_studio.db"))
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
CORS(app, supports_credentials=True)

SITE_URL = os.environ.get("SITE_URL", "https://ai-tts-website-online.onrender.com").rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

PLANS = {
    "starter": {"name": "Starter", "price": 100, "days": 5, "characters": 15000},
    "basic": {"name": "Basic", "price": 200, "days": 10, "characters": 35000},
    "pro": {"name": "Pro", "price": 600, "days": 30, "characters": 100000},
}
FREE_CHARACTERS = 1000
PREVIEW_MAX_CHARS = 220  # short demo; keeps preview limited server-side

# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            profile_photo TEXT DEFAULT '',
            auth_provider TEXT NOT NULL DEFAULT 'email',
            created_at TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            character_balance INTEGER NOT NULL DEFAULT 1000 CHECK(character_balance >= 0),
            plan_started_at TEXT,
            plan_expiry TEXT,
            free_bonus_claimed INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            filename TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            voice TEXT NOT NULL,
            style TEXT NOT NULL,
            characters INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            charged INTEGER NOT NULL DEFAULT 0,
            charged_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS usage_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            reference_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            provider TEXT NOT NULL DEFAULT 'razorpay',
            provider_order_id TEXT UNIQUE,
            provider_payment_id TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL,
            verified_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)

init_db()

# =========================================================
# HELPERS
# =========================================================

def safe_filename(name):
    name = (name or "").strip() or "AI-Voice"
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "-", name)
    return name[:80]


def prepare_text(text, style):
    text = (text or "").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if style == "horror":
        text = text.replace("...", "…")
    elif style == "emotional":
        text = re.sub(r"\.\s+", ". ", text)
    elif style == "kids":
        text = re.sub(r"!+", "!", text)
    return text


def split_text(text, max_chars=2800):
    paragraphs = text.split("\n")
    chunks, current = [], ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(current) + len(paragraph) + 1 <= max_chars:
            current += ("\n" if current else "") + paragraph
        else:
            if current:
                chunks.append(current)
            if len(paragraph) > max_chars:
                sentences = re.split(r"(?<=[.!?])\s+", paragraph)
                temp = ""
                for sentence in sentences:
                    if len(temp) + len(sentence) + 1 <= max_chars:
                        temp += (" " if temp else "") + sentence
                    else:
                        if temp:
                            chunks.append(temp)
                        temp = sentence
                current = temp
            else:
                current = paragraph
    if current:
        chunks.append(current)
    return chunks


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"success": False, "error": "Please login first."}), 401
        return fn(*args, **kwargs)
    return wrapper


def user_json(user):
    return {
        "id": user["public_id"], "name": user["name"], "email": user["email"],
        "profile_photo": user["profile_photo"], "plan": user["plan"],
        "character_balance": user["character_balance"], "plan_started_at": user["plan_started_at"],
        "plan_expiry": user["plan_expiry"], "created_at": user["created_at"]
    }


def generate_mp3(text, voice, rate, pitch, output_file):
    async def run():
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        await communicate.save(output_file)
    asyncio.run(run())
    if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
        raise Exception("Audio file was not created correctly.")


def http_json(url, data=None, headers=None, method=None):
    encoded = None
    if data is not None:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))

# =========================================================
# PUBLIC / SEO
# =========================================================

@app.route("/")
def home():
    index_file = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(index_file):
        return "ERROR: index.html was not found.", 404
    return send_file(index_file)

@app.route("/sitemap.xml")
def sitemap():
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n<url><loc>{SITE_URL}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n</urlset>'''
    return xml, 200, {"Content-Type": "application/xml"}

@app.route("/robots.txt")
def robots():
    return f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n", 200, {"Content-Type": "text/plain"}

@app.route("/health")
def health():
    return jsonify({"success": True, "status": "online", "service": "AI Voice Studio Pro"})

# =========================================================
# AUTH: EMAIL
# =========================================================

@app.route("/auth/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not name or not email or len(password) < 8:
        return jsonify({"success": False, "error": "Name, valid email and password of at least 8 characters are required."}), 400
    try:
        with db() as conn:
            cur = conn.execute("""INSERT INTO users(public_id,name,email,password_hash,auth_provider,created_at,plan,character_balance,free_bonus_claimed)
                                VALUES(?,?,?,?,?,?,?,?,1)""",
                               (str(uuid.uuid4()), name, email, generate_password_hash(password), "email", now_iso(), "free", FREE_CHARACTERS))
            uid = cur.lastrowid
            conn.execute("INSERT INTO usage_ledger(user_id,amount,reason,reference_id,created_at) VALUES(?,?,?,?,?)",
                         (uid, FREE_CHARACTERS, "Free signup bonus", "signup", now_iso()))
        session.permanent = True
        session["user_id"] = uid
        return jsonify({"success": True, "user": user_json(current_user())})
    except sqlite3.IntegrityError:
        return jsonify({"success": False, "error": "An account with this email already exists."}), 409

@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return jsonify({"success": False, "error": "Invalid email or password."}), 401
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify({"success": True, "user": user_json(user)})

@app.route("/auth/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/auth/me")
def me():
    user = current_user()
    return jsonify({"success": True, "logged_in": bool(user), "user": user_json(user) if user else None})

# =========================================================
# AUTH: GOOGLE OAUTH
# =========================================================

@app.route("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({"success": False, "error": "Google Login is not configured on the server."}), 503
    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": SITE_URL + "/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))

@app.route("/auth/google/callback")
def google_callback():
    if request.args.get("state") != session.pop("google_oauth_state", None):
        return jsonify({"success": False, "error": "Invalid Google OAuth state."}), 400
    code = request.args.get("code")
    if not code:
        return jsonify({"success": False, "error": "Google Login was cancelled or failed."}), 400
    try:
        token = http_json("https://oauth2.googleapis.com/token", {
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": SITE_URL + "/auth/google/callback", "grant_type": "authorization_code"
        })
        info = http_json("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": "Bearer " + token["access_token"]})
        email = (info.get("email") or "").lower()
        if not email or not info.get("email_verified", False):
            return jsonify({"success": False, "error": "Google email could not be verified."}), 400
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user:
                cur = conn.execute("""INSERT INTO users(public_id,name,email,profile_photo,auth_provider,created_at,plan,character_balance,free_bonus_claimed)
                                    VALUES(?,?,?,?,?,?,?,?,1)""",
                                   (str(uuid.uuid4()), info.get("name", ""), email, info.get("picture", ""), "google", now_iso(), "free", FREE_CHARACTERS))
                uid = cur.lastrowid
                conn.execute("INSERT INTO usage_ledger(user_id,amount,reason,reference_id,created_at) VALUES(?,?,?,?,?)",
                             (uid, FREE_CHARACTERS, "Free signup bonus", "signup", now_iso()))
            else:
                uid = user["id"]
                conn.execute("UPDATE users SET name=?, profile_photo=? WHERE id=?", (info.get("name", user["name"]), info.get("picture", user["profile_photo"]), uid))
        session.permanent = True
        session["user_id"] = uid
        return redirect(SITE_URL + "/?login=success")
    except Exception as e:
        return jsonify({"success": False, "error": "Google Login failed: " + str(e)}), 500

# =========================================================
# VOICES
# =========================================================

@app.route("/voices")
def voices():
    try:
        async def get_voices():
            return await edge_tts.list_voices()
        voice_list = asyncio.run(get_voices())
        allowed_locales = ["en-US","en-GB","en-AU","en-IN","hi-IN","mr-IN","ta-IN","te-IN","bn-IN","gu-IN","kn-IN","ml-IN"]
        result = []
        for item in voice_list:
            if item.get("Locale", "") not in allowed_locales:
                continue
            result.append({"name": item.get("ShortName", ""), "display_name": item.get("FriendlyName", item.get("ShortName", "")),
                           "locale": item.get("Locale", ""), "gender": item.get("Gender", "Unknown")})
        result.sort(key=lambda x: (x["locale"], x["gender"], x["name"]))
        return jsonify({"success": True, "count": len(result), "voices": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# =========================================================
# FREE PREVIEW - NO DEDUCTION
# =========================================================

@app.route("/preview", methods=["POST"])
@login_required
def preview():
    try:
        text = (request.form.get("text") or "").strip()
        voice = request.form.get("voice", "en-US-GuyNeural")
        rate = request.form.get("rate", "+0%")
        pitch = request.form.get("pitch", "+0Hz")
        style = request.form.get("style", "natural")
        if not text:
            return jsonify({"success": False, "error": "Please enter some text."}), 400
        preview_text = prepare_text(text[:PREVIEW_MAX_CHARS], style)
        filename = "preview-" + str(uuid.uuid4())[:10] + ".mp3"
        output_file = os.path.join(OUTPUT_FOLDER, filename)
        generate_mp3(preview_text, voice, rate, pitch, output_file)
        return jsonify({"success": True, "preview_url": "/preview-audio/" + filename, "characters_charged": 0})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/preview-audio/<filename>")
@login_required
def preview_audio(filename):
    filename = os.path.basename(filename)
    if not filename.startswith("preview-"):
        return "Not found", 404
    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return "Audio file not found.", 404
    return send_file(path, mimetype="audio/mpeg")

# =========================================================
# GENERATE FULL AUDIO - NO CHARGE YET
# =========================================================

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    try:
        user = current_user()
        text = (request.form.get("text") or "").strip()
        voice = request.form.get("voice", "en-US-GuyNeural")
        rate = request.form.get("rate", "+0%")
        pitch = request.form.get("pitch", "+0Hz")
        style = request.form.get("style", "natural")
        custom_name = request.form.get("filename", "AI-Voice")
        if not text:
            return jsonify({"success": False, "error": "Please enter some text."}), 400
        if len(text) > 50000:
            return jsonify({"success": False, "error": "Maximum 50,000 characters are allowed."}), 400
        if not voice:
            return jsonify({"success": False, "error": "Please select a voice."}), 400
        if len(text) > user["character_balance"]:
            return jsonify({"success": False, "error": "Not enough characters. Please upgrade your plan.",
                            "required": len(text), "available": user["character_balance"]}), 402
        processed_text = prepare_text(text, style)
        final_text = "\n\n".join(split_text(processed_text))
        safe_name = safe_filename(custom_name)
        generation_id = str(uuid.uuid4())
        filename = f"{safe_name}-{generation_id[:8]}.mp3"
        output_file = os.path.join(OUTPUT_FOLDER, filename)
        generate_mp3(final_text, voice, rate, pitch, output_file)
        with db() as conn:
            conn.execute("""INSERT INTO generations(generation_id,user_id,filename,title,voice,style,characters,created_at,charged)
                          VALUES(?,?,?,?,?,?,?,?,0)""",
                         (generation_id, user["id"], filename, safe_name, voice, style, len(text), now_iso()))
        return jsonify({"success": True, "generation_id": generation_id, "characters": len(text),
                        "charged": False, "preview_message": "Full audio generated. Characters will be deducted on first download.",
                        "download_url": "/download/" + filename})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# =========================================================
# HISTORY / ACCOUNT
# =========================================================

@app.route("/history")
@login_required
def history():
    user = current_user()
    with db() as conn:
        rows = conn.execute("SELECT * FROM generations WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
    items = [{"id": r["generation_id"], "filename": r["filename"], "title": r["title"], "voice": r["voice"],
              "style": r["style"], "created_at": r["created_at"], "characters": r["characters"], "charged": bool(r["charged"]),
              "download_url": "/download/" + r["filename"]} for r in rows]
    return jsonify({"success": True, "history": items})

@app.route("/history/clear", methods=["POST"])
@login_required
def clear_history():
    user = current_user()
    with db() as conn:
        rows = conn.execute("SELECT filename FROM generations WHERE user_id=?", (user["id"],)).fetchall()
        conn.execute("DELETE FROM generations WHERE user_id=?", (user["id"],))
    for r in rows:
        path = os.path.join(OUTPUT_FOLDER, os.path.basename(r["filename"]))
        try:
            if os.path.exists(path): os.remove(path)
        except OSError:
            pass
    return jsonify({"success": True})

@app.route("/account")
@login_required
def account():
    user = current_user()
    with db() as conn:
        ledger = conn.execute("SELECT amount,reason,reference_id,created_at FROM usage_ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (user["id"],)).fetchall()
        payments = conn.execute("SELECT plan_id,amount,currency,status,created_at,verified_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
    return jsonify({"success": True, "user": user_json(user), "usage_history": [dict(x) for x in ledger], "payment_history": [dict(x) for x in payments]})

# =========================================================
# AUDIO / DOWNLOAD - CHARGE EXACTLY ONCE
# =========================================================

@app.route("/audio/<filename>")
@login_required
def audio(filename):
    user = current_user()
    filename = os.path.basename(filename)
    with db() as conn:
        gen = conn.execute("SELECT * FROM generations WHERE filename=? AND user_id=?", (filename, user["id"])).fetchone()
    if not gen or not gen["charged"]:
        return jsonify({"success": False, "error": "Full audio is locked. Download/unlock it first."}), 403
    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path): return "Audio file not found.", 404
    return send_file(path, mimetype="audio/mpeg")

@app.route("/download/<filename>")
@login_required
def download(filename):
    user = current_user()
    filename = os.path.basename(filename)
    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return "Audio file not found.", 404
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        gen = conn.execute("SELECT * FROM generations WHERE filename=? AND user_id=?", (filename, user["id"])).fetchone()
        if not gen:
            conn.rollback()
            return jsonify({"success": False, "error": "Audio not found for this account."}), 404
        if not gen["charged"]:
            fresh_user = conn.execute("SELECT character_balance FROM users WHERE id=?", (user["id"],)).fetchone()
            needed = gen["characters"]
            if fresh_user["character_balance"] < needed:
                conn.rollback()
                return jsonify({"success": False, "error": "Not enough characters. Please upgrade your plan.",
                                "required": needed, "available": fresh_user["character_balance"]}), 402
            conn.execute("UPDATE users SET character_balance=character_balance-? WHERE id=? AND character_balance>=?", (needed, user["id"], needed))
            conn.execute("UPDATE generations SET charged=1, charged_at=? WHERE id=? AND charged=0", (now_iso(), gen["id"]))
            conn.execute("INSERT INTO usage_ledger(user_id,amount,reason,reference_id,created_at) VALUES(?,?,?,?,?)",
                         (user["id"], -needed, "Audio download", gen["generation_id"], now_iso()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return send_file(path, mimetype="audio/mpeg", as_attachment=True, download_name=filename)

# =========================================================
# PLANS / RAZORPAY
# =========================================================

@app.route("/plans")
def plans():
    return jsonify({"success": True, "free_signup_characters": FREE_CHARACTERS, "plans": PLANS})


def razorpay_request(path, payload):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay is not configured.")
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request("https://api.razorpay.com/v1" + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())

@app.route("/payments/create-order", methods=["POST"])
@login_required
def create_order():
    data = request.get_json(silent=True) or request.form
    plan_id = (data.get("plan_id") or "").lower()
    plan = PLANS.get(plan_id)
    if not plan:
        return jsonify({"success": False, "error": "Invalid plan."}), 400
    try:
        user = current_user()
        order = razorpay_request("/orders", {"amount": plan["price"] * 100, "currency": "INR", "receipt": "avs-" + str(uuid.uuid4())[:12]})
        with db() as conn:
            conn.execute("INSERT INTO payments(user_id,plan_id,amount,currency,provider_order_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
                         (user["id"], plan_id, plan["price"], "INR", order["id"], "created", now_iso()))
        return jsonify({"success": True, "key_id": RAZORPAY_KEY_ID, "order_id": order["id"], "amount": plan["price"] * 100,
                        "currency": "INR", "plan": plan_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/payments/verify", methods=["POST"])
@login_required
def verify_payment():
    data = request.get_json(silent=True) or {}
    order_id = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature = data.get("razorpay_signature", "")
    if not all([order_id, payment_id, signature, RAZORPAY_KEY_SECRET]):
        return jsonify({"success": False, "error": "Missing payment verification data."}), 400
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"success": False, "error": "Payment verification failed."}), 400
    user = current_user()
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pay = conn.execute("SELECT * FROM payments WHERE provider_order_id=? AND user_id=?", (order_id, user["id"])).fetchone()
        if not pay:
            conn.rollback(); return jsonify({"success": False, "error": "Payment order not found."}), 404
        if pay["status"] == "verified":
            conn.commit(); return jsonify({"success": True, "already_verified": True, "user": user_json(current_user())})
        plan = PLANS[pay["plan_id"]]
        start = datetime.now(timezone.utc)
        expiry = start + timedelta(days=plan["days"])
        conn.execute("UPDATE payments SET provider_payment_id=?, status='verified', verified_at=? WHERE id=?",
                     (payment_id, now_iso(), pay["id"]))
        conn.execute("UPDATE users SET plan=?, character_balance=character_balance+?, plan_started_at=?, plan_expiry=? WHERE id=?",
                     (pay["plan_id"], plan["characters"], start.isoformat(), expiry.isoformat(), user["id"]))
        conn.execute("INSERT INTO usage_ledger(user_id,amount,reason,reference_id,created_at) VALUES(?,?,?,?,?)",
                     (user["id"], plan["characters"], "Plan purchase: " + plan["name"], payment_id, now_iso()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"success": False, "error": "This payment has already been used."}), 409
    except Exception as e:
        conn.rollback(); return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "user": user_json(current_user())})

# =========================================================
# ERRORS / START
# =========================================================

@app.errorhandler(404)
def page_not_found(error):
    return jsonify({"success": False, "error": "Page not found."}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"success": False, "error": "Internal server error."}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
