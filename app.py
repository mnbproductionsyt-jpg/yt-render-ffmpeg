
import os
import uuid
import time
import tempfile
import subprocess
from urllib.request import urlopen, Request

from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader as cl_uploader

app = Flask(__name__)

# -------- Config --------
# Cloudinary via CLOUDINARY_URL = cloudinary://API_KEY:API_SECRET@CLOUD_NAME
cloudinary_url = os.environ.get("CLOUDINARY_URL", "").strip()
if cloudinary_url:
    cloudinary.config(cloudinary_url=cloudinary_url)

# -------- Health --------
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "yt-render-ffmpeg"}), 200

@app.route("/health", methods=["GET", "HEAD"])
@app.route("/_ah/health", methods=["GET", "HEAD"])
def health():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

# -------- Helpers --------
def clean_url(u: str) -> str:
    if u is None:
        return ""
    s = str(u).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s  # usamos la URL exactamente como la mandas

def fetch_to_file(url: str, suffix: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=120) as resp:
        data = resp.read()
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path

# -------- Render --------
@app.post("/render")
def render():
    """
    JSON esperado:
    {
      "size": {"w":1280,"h":720},
      "fps": 24,
      "audio_url": "https://.../audio.mp3",
      "scenes": [
        {"image_url":"https://.../img1.jpg","seconds":6},
        {"image_url":"https://.../img2.jpg","seconds":6}
      ]
    }
    """
    try:
        data = request.get_json(force=True)
        size = data.get("size") or {}
        W = int(size.get("w", 1280))
        H = int(size.get("h", 720))
        fps = int(data.get("fps", 24))
        audio_url = clean_url(data.get("audio_url", ""))
        scenes = data.get("scenes") or []
        if not scenes:
            return jsonify({"status": "error", "error": "scenes vacío"}), 400
        if not audio_url:
            return jsonify({"status": "error", "error": "audio_url vacío"}), 400
    except Exception as e:
        return jsonify({"status": "error", "error": f"Payload inválido: {e}"}), 400

    workdir = tempfile.mkdtemp(prefix="render_")
    seg_files, local_imgs = [], []
    local_audio = None
    concat_path = os.path.join(workdir, "concat.mp4")
    out_path = os.path.join(workdir, f"{uuid.uuid4().hex}.mp4")

    try:
        # Descargar audio
        local_audio = fetch_to_file(audio_url, ".mp3")

        # Generar segmentos de video por imagen
        for i, s in enumerate(scenes, start=1):
            img_url = clean_url(s.get("image_url", ""))
            secs = float(s.get("seconds", 5))
            if not img_url or secs <= 0:
                continue
            img_path = fetch_to_file(img_url, f"_{i:02d}.jpg")
            local_imgs.append(img_path)

            vf = (
                f"scale=w={W}:h={H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
            seg_path = os.path.join(workdir, f"seg_{i:02d}.mp4")
            cmd_seg = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-t", f"{secs}",
                "-r", f"{fps}",
                "-i", img_path,
                "-vf", vf,
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                seg_path
            ]
            subprocess.check_output(cmd_seg, stderr=subprocess.STDOUT)
            seg_files.append(seg_path)

        if not seg_files:
            return jsonify({"status": "error", "error": "No se generaron segmentos"}), 400

        # Concatenar segmentos
        list_path = os.path.join(workdir, "list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for s in seg_files:
                f.write(f"file '{s}'\n")
        cmd_concat = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            concat_path
        ]
        subprocess.check_output(cmd_concat, stderr=subprocess.STDOUT)

        # Multiplexar con audio (recorta al menor con -shortest)
        cmd_mux = [
            "ffmpeg", "-y",
            "-i", concat_path,
            "-i", local_audio,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            out_path
        ]
        subprocess.check_output(cmd_mux, stderr=subprocess.STDOUT)

        # Subir a Cloudinary como video
        if not cloudinary_url:
            return jsonify({"status": "error", "error": "CLOUDINARY_URL no configurado"}), 500

        public_id = f"ytauto/{time.strftime('%Y%m%d')}/{uuid.uuid4().hex}"
        up = cl_uploader.upload(
            out_path,
            resource_type="video",
            public_id=public_id,
            overwrite=True,
            use_filename=False,
            unique_filename=False
        )
        secure_url = up.get("secure_url")

        return jsonify({
            "status": "ok",
            "video_url": secure_url,
            "meta": {"w": W, "h": H, "fps": fps, "scenes": len(seg_files)}
        }), 200

    except subprocess.CalledProcessError as e:
        err = e.output.decode("utf-8", errors="ignore") if e.output else str(e)
        return jsonify({"status": "error", "stage": "ffmpeg", "stderr": err}), 500
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        try:
            if local_audio and os.path.exists(local_audio):
                os.remove(local_audio)
            for p in local_imgs + seg_files:
                if os.path.exists(p):
                    os.remove(p)
            if os.path.exists(concat_path):
                os.remove(concat_path)
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

