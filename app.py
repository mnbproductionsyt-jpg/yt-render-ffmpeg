import os, tempfile, subprocess, uuid, time
from urllib.request import urlopen, Request
from urllib.parse import unquote
from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "yt-render-ffmpeg"}), 200

@app.get("/healthz")
def healthz():
    return "ok", 200


# Variables via entorno en Cloud Run:
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")                  # p.ej. "yt-auto-videos-123"
GCS_SIGNED_URL_TTL = int(os.environ.get("GCS_SIGNED_URL_TTL", "86400"))  # 24h
storage_client = storage.Client() if GCS_BUCKET else None
def clean_url(u: str) -> str:
    if u is None:
        return ""
    s = str(u).strip().strip('"').strip("'")
    s = unquote(s)           # decodifica %22, %20, etc.
    return s.replace(" ", "%20")  # espacios seguros


def fetch_to_file(url, suffix):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=120) as r, open(tmp.name, "wb") as f:
        f.write(r.read())
    return tmp.name

def build_concat_list(scenes):
    list_path = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    with open(list_path, "w", encoding="utf-8") as f:
        for s in scenes:
            f.write(f"file '{s['local_path']}'\n")
            f.write(f"duration {float(s['seconds'])}\n")
    return list_path

def upload_to_gcs(local_path, content_type="video/mp4"):
    assert storage_client and GCS_BUCKET, "GCS no configurado"
    blob_name = f"yt-auto/{time.strftime('%Y-%m')}/{uuid.uuid4().hex}.mp4"
    bucket = storage_client.bucket(GCS_BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path, content_type=content_type)
    url = blob.generate_signed_url(expiration=GCS_SIGNED_URL_TTL, method="GET")
    return url, f"gs://{GCS_BUCKET}/{blob_name}"

@app.post("/render")
def render():
    """
    Body JSON:
    {
      "size": {"w": 1280, "h": 720},  # opcional; default 1280x720
      "fps": 24,                      # opcional; default 24
      "audio_url": "https://.../voice.mp3",
      "scenes": [
        {"image_url": "...", "seconds": 6},
        ...
      ]
    }
    """
    data = request.get_json(force=True)
    w = int(data.get("size", {}).get("w", 1280))
    h = int(data.get("size", {}).get("h", 720))
    fps = int(data.get("fps", 24))
    audio_url = clean_url(data["audio_url"])
    scenes_in = data["scenes"]

    local_imgs = []
for i, s in enumerate(scenes_in, start=1):
    img_url = clean_url(s.get("image_url", ""))
    img_path = fetch_to_file(img_url, suffix=f"_{i:02d}.jpg")
    secs = float(s.get("seconds", 5))  # si no viene, usa 5s
    local_imgs.append({"local_path": img_path, "seconds": secs})

    audio_path = fetch_to_file(audio_url, suffix=".mp3")

    lst_path = build_concat_list(local_imgs)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=cover,crop={w}:{h}"
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    cmd = [
        "ffmpeg","-y",
        "-r", str(fps),
        "-safe","0","-f","concat","-i", lst_path,
        "-i", audio_path,
        "-shortest",
        "-vf", vf,
        "-c:v","libx264","-preset","veryfast","-crf","23",
        "-pix_fmt","yuv420p",
        "-c:a","aac",
        "-movflags","+faststart",
        out_path
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=55*60)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout en render (55 min). Divide el video o usa Jobs)."}), 504

    if proc.returncode != 0:
        return jsonify({"error": "FFmpeg falló", "stderr": proc.stderr[-2000:]}), 500

    if storage_client and GCS_BUCKET:
        signed_url, gspath = upload_to_gcs(out_path)
        return jsonify({"status":"ok","video_url":signed_url,"gcs_path":gspath,"fps":fps,"size":{"w":w,"h":h}})
    else:
        return jsonify({"error":"Configura GCS_BUCKET para URL firmada."}), 500
