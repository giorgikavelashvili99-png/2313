import os
import time
import uuid
import shutil
import sqlite3
import tempfile
import threading
import subprocess
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

# ffmpeg/ffprobe binaries — on Render these come from the Docker image (see Dockerfile).
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")

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


def init_compress_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS compress_jobs (
            id TEXT PRIMARY KEY,
            input_file_id TEXT NOT NULL,
            output_file_id TEXT,
            filename TEXT,
            target_mb REAL,
            codec TEXT,
            fps TEXT,
            status TEXT DEFAULT 'queued',
            progress REAL DEFAULT 0,
            message TEXT,
            original_size INTEGER,
            output_size INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
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
# Compression (server-side ffmpeg, runs in a background thread)
# ---------------------------------------------------------------------------
def get_video_duration(path):
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def calc_video_bitrate_kbps(duration_sec, target_mb, audio_kbps=160):
    """Back-solve the video bitrate needed to hit a target file size,
    after reserving space for the audio track."""
    target_bits = target_mb * 8 * 1024 * 1024
    audio_bits = audio_kbps * 1000 * duration_sec
    video_bitrate = (target_bits - audio_bits) / duration_sec
    return max(int(video_bitrate / 1000), 100)  # floor at 100kbps so it never goes negative/absurd


def run_compress_job(job_id, input_file_id, target_mb, codec, fps_choice):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    tmp_dir = None

    def set_job(**fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE compress_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
        conn.commit()

    try:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (input_file_id,)).fetchone()
        if not row or row["deleted"]:
            set_job(status="error", error="input_not_found")
            return

        set_job(status="downloading", progress=2, message="ვიდეოს გადმოწერა სერვერზე…")
        tmp_dir = tempfile.mkdtemp(prefix="moonpress_")
        in_ext = os.path.splitext(row["filename"])[1] or ".mp4"
        in_path = os.path.join(tmp_dir, "input" + in_ext)
        out_path = os.path.join(tmp_dir, "output.mp4")

        s3.download_file(R2_BUCKET, row["object_key"], in_path)

        duration = get_video_duration(in_path)
        if not duration or duration <= 0:
            set_job(status="error", error="bad_duration")
            return

        vbitrate = calc_video_bitrate_kbps(duration, target_mb)
        encoder = "libx265" if codec == "x265" else "libx264"

        cmd = [
            FFMPEG_BIN, "-y", "-i", in_path,
            "-c:v", encoder, "-b:v", f"{vbitrate}k",
            "-maxrate", f"{int(vbitrate * 1.45)}k", "-bufsize", f"{int(vbitrate * 2)}k",
            "-preset", "fast", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
        ]
        if fps_choice and fps_choice != "source":
            cmd += ["-r", str(fps_choice)]
        cmd += ["-progress", "pipe:1", "-nostats", out_path]

        set_job(status="compressing", progress=5, message="შეკუმშვა მიმდინარეობს…")

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    out_ms = int(line.split("=", 1)[1])
                    pct = min(97.0, 5 + (out_ms / 1_000_000 / duration) * 90)
                    set_job(progress=pct, message=f"შეკუმშვა: {pct:.0f}%")
                except Exception:
                    pass
        proc.wait()

        if proc.returncode != 0 or not os.path.exists(out_path):
            set_job(status="error", error="ffmpeg_failed")
            return

        set_job(status="uploading", progress=98, message="მზა ვიდეოს ატვირთვა…")

        out_size = os.path.getsize(out_path)
        output_file_id = uuid.uuid4().hex[:12]
        base_name = os.path.splitext(row["filename"])[0]
        out_filename = f"{base_name}_compressed.mp4"
        object_key = f"uploads/{output_file_id}/{out_filename}"

        with open(out_path, "rb") as f:
            s3.upload_fileobj(f, R2_BUCKET, object_key, ExtraArgs={"ContentType": "video/mp4"})

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=EXPIRY_HOURS)
        conn.execute(
            "INSERT INTO files (id, object_key, filename, content_type, size, email, created_at, expires_at, finalized) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (output_file_id, object_key, out_filename, "video/mp4", out_size, None, now.isoformat(), expires_at.isoformat()),
        )
        conn.commit()

        # the raw original was only needed for encoding — drop it now, keep just the result
        try:
            s3.delete_object(Bucket=R2_BUCKET, Key=row["object_key"])
            conn.execute("UPDATE files SET deleted = 1 WHERE id = ?", (input_file_id,))
            conn.commit()
        except Exception as e:
            print(f"[compress] failed to clean up original {row['object_key']}: {e}")

        set_job(
            status="done", progress=100, message="მზადაა",
            output_file_id=output_file_id, output_size=out_size,
        )

    except Exception as e:
        set_job(status="error", error=str(e))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        conn.close()


# ---------------------------------------------------------------------------
# Routes — sharing (unchanged)
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


# ---------------------------------------------------------------------------
# Routes — compression (new)
# ---------------------------------------------------------------------------
@app.route("/compress")
def compress_page():
    return render_template("compress.html", expiry_hours=EXPIRY_HOURS)


@app.route("/api/compress/start", methods=["POST"])
def compress_start():
    data = request.get_json(force=True)
    file_id = data.get("file_id")
    codec = data.get("codec", "x264")
    fps_choice = data.get("fps", "source")

    try:
        target_mb = float(data.get("target_mb", 99))
    except (TypeError, ValueError):
        return jsonify({"error": "bad_target"}), 400

    if codec not in ("x264", "x265"):
        return jsonify({"error": "bad_codec"}), 400
    if target_mb <= 0 or target_mb > 4000:
        return jsonify({"error": "bad_target"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row or row["deleted"]:
        return jsonify({"error": "not_found"}), 404

    job_id = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO compress_jobs "
        "(id, input_file_id, filename, target_mb, codec, fps, status, progress, original_size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
        (job_id, file_id, row["filename"], target_mb, codec, fps_choice, row["size"], datetime.utcnow().isoformat()),
    )
    db.commit()

    threading.Thread(
        target=run_compress_job,
        args=(job_id, file_id, target_mb, codec, fps_choice),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/compress/status/<job_id>")
def compress_status(job_id):
    db = get_db()
    row = db.execute("SELECT * FROM compress_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404

    resp = {
        "status": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "original_size": row["original_size"],
        "output_size": row["output_size"],
        "error": row["error"],
    }
    if row["status"] == "done" and row["output_file_id"]:
        resp["file_id"] = row["output_file_id"]
        resp["download_page"] = f"/d/{row['output_file_id']}"

    return jsonify(resp)


init_db()
init_compress_table()
cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
