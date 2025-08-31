import os
import uuid
import time
import tempfile
import subprocess
from urllib.request import urlopen, Request
from urllib.parse import unquote

from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)

# ---------------- Config ----------------
GCS_BUCKET = os.environ.get("GCS_BUCKET", "").strip()
GCS_SIGNED_URL_TTL = int(os.environ.get("GCS_SIGNED_URL_TTL", "86400"))  # 24h
storage_client = storage.Client() if GCS_BUCKET else None


# ---------------- Health ----------------
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "yt-render-ffmpeg"}), 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.route("/health", methods=["GET", "HEAD"])
@app.route("/_ah/health", methods=["GET", "HEAD"])
def health_alt():
    return "ok", 200


# ---------------- Helpers ----------------
def clean_url(u: str) -> str:
    if u is None:
        return ""
    s = str(u).strip().strip('"').strip("'")
    s = unquote(s)
    return s.replace(" ", "%20")

def fetch_to_file(url: str, suffix: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req) as resp:
        data = resp.read()
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path

def upload_to_gcs(local_path: str, content_type: str = "video/mp4") -> str:
    if not storage_client or not GCS_BUCKET:
        raise RuntimeError("GCS no configurado. Falta GCS_BUCKET.")
    key = f"renders/{time.strftime('%Y%m%d')}/{uuid.uuid4().hex}.mp4"
    bucket = storage_client.bucket(GCS_BUCKET)
    blob = bucket.blob(key)
    blob.upload_from_filename(local_path, content_type=content_type)
    url = blob.generate_signed_url(
        version="v4",
        expiration=GCS_SIGNED_URL_TTL,
        method="GET",
        response_disposition="attachment; filename=video.mp4",
    )
    return url


# ---------------- Render ----------------
@app.post("/render")
def render():
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"status": "error", "error": "Invalid JSON body"}), 400

    try:
        size = data.get("size") or {}
        W = int(size.get("w", 1280))
        H = int(size.get("h", 720))
        fps = int(data.get("fps", 24))
        audio_url = clean_url(data.get("audio_url", ""))
        scenes = data.get("scenes") or []
        if not scenes:
            return jsonify({"status":"error","error":"scenes vacío"}), 400
        if not audio_url:
            return jsonify({"status":"error","error":"audio_url vacío"}), 400
    except Exception as e:
        return jsonify({"status":"error","error": f"Payload inválido: {e}"}), 400

    workdir = tempfile.mkdtemp(prefix="render_")
    seg_files, local_imgs = [], []
    local_audio = None
    out_path = os.path.join(workdir, "out.mp4")

    try:
        # Descargar audio
        local_audio = fetch_to_file(audio_url, ".mp3")

        # Generar segmentos de video
        for i, s in enumerate(scenes, start=1):
            img_url = clean_url(s.get("image_url", ""))
            secs = float(s.get("seconds", 5))
            if not img_url or secs <= 0:
                continue
            img_path = fetch_to_file(img_url, f"_{i:02d}.jpg")
            local_imgs.append(img_path)

            seg_path = os.path.join(workdir, f"seg_{i:02d}.mp4")
            vf = (
                f"scale=w={W}:h={H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
            cmd_seg = [
                "ffmpeg","-y","-loop","1","-t",f"{secs}","-r",f"{fps}",
                "-i",img_path,"-vf",vf,"-c:v","libx264",
                "-pix_fmt","yuv420p","-movflags","+faststart",seg_path
            ]
            subprocess.check_output(cmd_seg, stderr=subprocess.STDOUT)
            seg_files.append(seg_path)

        if not seg_files:
            return jsonify({"status":"error","error":"No se generaron segmentos"}), 400

        # Concatenar segmentos
        list_path = os.path.join(workdir, "list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for s in seg_files:
                f.write(f"file '{s}'\n")
        concat_path = os.path.join(workdir, "concat.mp4")
        cmd_concat = [
            "ffmpeg","-y","-f","concat","-safe","0","-i",list_path,"-c","copy",concat_path
        ]
        subprocess.check_output(cmd_concat, stderr=subprocess.STDOUT)

        # Mezclar audio
        cmd_mux = [
            "ffmpeg","-y","-i",concat_path,"-i",local_audio,
            "-c:v","copy","-c:a","aac","-b:a","192k","-shortest",out_path
        ]
        subprocess.check_output(cmd_mux, stderr=subprocess.STDOUT)

        # Subir a GCS
        video_url = upload_to_gcs(out_path)

        return jsonify({"status":"ok","video_url":video_url,"meta":{"w":W,"h":H,"fps":fps,"scenes":len(seg_files)}}), 200

    except subprocess.CalledProcessError as e:
        err = e.output.decode("utf-8", errors="ignore") if e.output else str(e)
        return jsonify({"status":"error","stage":"ffmpeg","stderr":err}), 500
    except Exception as e:
        return jsonify({"status":"error","error": str(e)}), 500
    finally:
        try:
            if local_audio and os.path.exists(local_audio): os.remove(local_audio)
            for p in local_imgs:
                if os.path.exists(p): os.remove(p)
            for p in seg_files:
                if os.path.exists(p): os.remove(p)
        except Exception:
            pass


# ---------------- Dev ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
