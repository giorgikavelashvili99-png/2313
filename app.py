import os
import time
import uuid
import sqlite3
import threading
from datetime import datetime, timedelta

import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, render_template, g, abort

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env for local testing only — never commit that file
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration (all via environment variables — set these in Render.com)
# ---------------------------------------------------------------------------
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "556af970dee894bf159fb78830fb44e8")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "moonfade")
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

if not R2_ACCOUNT_ID or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
    missing = [
        name for name, val in [
            ("R2_ACCOUNT_ID", R2_ACCOUNT_ID),
            ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
        ] if not val
    ]
    print(f"[config] Missing/empty: {', '.join(missing)}. Check that .env sits next to app.py and python-dotenv is installed.")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
EXPIRY_HOURS = float(os.environ.get("EXPIRY_HOURS", "6"))

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")

DB_PATH = os.environ.get("DB_PATH", "moonfade.db")

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024 * 1024)))  # 4GB default

app = Flask(__name__)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", region_name="auto"),
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            object_key TEXT NOT NULL,
            filename TEXT NOT NULL,
            content_type TEXT,
            size INTEGER,
            email TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            finalized INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Cleanup worker — deletes expired objects from R2 and marks rows deleted.
# Runs in a background thread inside the same process (Render free web
# services stay awake while receiving traffic; this is a safety layer on
# top of an R2 lifecycle rule, which should also be configured as a backstop).
# ---------------------------------------------------------------------------
def cleanup_loop():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            now = datetime.utcnow().isoformat()
            rows = conn.execute(
                "SELECT id, object_key FROM files WHERE expires_at < ? AND deleted = 0",
                (now,),
            ).fetchall()
            for row in rows:
                try:
                    s3.delete_object(Bucket=R2_BUCKET, Key=row["object_key"])
                except Exception as e:
                    print(f"[cleanup] failed to delete {row['object_key']}: {e}")
                conn.execute("UPDATE files SET deleted = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[cleanup] loop error: {e}")
        time.sleep(600)  # every 10 minutes


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(to_email, link, filename, expires_at):
    if not RESEND_API_KEY:
        print("[email] RESEND_API_KEY not set, skipping send")
        return False
    import requests

    hours_left = EXPIRY_HOURS
    html = f"""
    <div style="background:#0B1220;padding:40px;font-family:sans-serif;color:#F2EFE9;">
      <h2 style="color:#E8B75D;">შენ გამოგიგზავნეს ვიდეო</h2>
      <p>ფაილი: <strong>{filename}</strong></p>
      <p>ბმული აქტიურია <strong>{hours_left:.0f} საათის</strong> განმავლობაში ატვირთვის მომენტიდან.</p>
      <p><a href="{link}" style="display:inline-block;background:#E8B75D;color:#0B1220;
         padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold;">
         ვიდეოს გადმოწერა</a></p>
      <p style="color:#8B93A0;font-size:12px;">ბმული ავტომატურად გაუქმდება ვადის ამოწურვის შემდეგ.</p>
    </div>
    """
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM,
                "to": [to_email],
                "subject": f"ვიდეო მოგელოდებათ — {filename}",
                "html": html,
            },
            timeout=15,
        )
        return resp.status_code < 300
    except Exception as e:
        print(f"[email] send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", expiry_hours=EXPIRY_HOURS)


@app.route("/api/request-upload", methods=["POST"])
def request_upload():
    data = request.get_json(force=True)
    filename = data.get("filename", "video")
    content_type = data.get("content_type", "application/octet-stream")
    size = int(data.get("size", 0))

    if size <= 0 or size > MAX_UPLOAD_BYTES:
        return jsonify({"error": "invalid_size"}), 400

    file_id = uuid.uuid4().hex[:12]
    object_key = f"uploads/{file_id}/{filename}"
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=EXPIRY_HOURS)

    db = get_db()
    db.execute(
        "INSERT INTO files (id, object_key, filename, content_type, size, email, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (file_id, object_key, filename, content_type, size, None, now.isoformat(), expires_at.isoformat()),
    )
    db.commit()

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET, "Key": object_key, "ContentType": content_type},
        ExpiresIn=3600,
    )

    return jsonify({"file_id": file_id, "upload_url": upload_url})


@app.route("/api/finalize", methods=["POST"])
def finalize():
    data = request.get_json(force=True)
    file_id = data.get("file_id")
    email = data.get("email")

    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404

    db.execute("UPDATE files SET finalized = 1, email = ? WHERE id = ?", (email, file_id))
    db.commit()

    link = f"{BASE_URL}/d/{file_id}"

    if email:
        send_email(email, link, row["filename"], row["expires_at"])

    return jsonify({"link": link, "expires_at": row["expires_at"]})


@app.route("/d/<file_id>")
def download_page(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row or row["deleted"]:
        return render_template("expired.html"), 410

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.utcnow() > expires_at:
        return render_template("expired.html"), 410

    return render_template(
        "download.html",
        file_id=file_id,
        filename=row["filename"],
        size=row["size"],
        expires_at=row["expires_at"],
    )


@app.route("/api/download-url/<file_id>")
def download_url(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row or row["deleted"]:
        return jsonify({"error": "expired"}), 410

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.utcnow() > expires_at:
        return jsonify({"error": "expired"}), 410

    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": row["object_key"],
            "ResponseContentDisposition": f'attachment; filename="{row["filename"]}"',
        },
        ExpiresIn=300,
    )
    return jsonify({"url": url, "expires_at": row["expires_at"], "filename": row["filename"]})


@app.route("/api/status/<file_id>")
def status(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row or row["deleted"]:
        return jsonify({"error": "expired"}), 410
    return jsonify({"expires_at": row["expires_at"], "filename": row["filename"], "size": row["size"]})


init_db()
cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
