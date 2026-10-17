import os
import json
import time
import shutil
import sqlite3
import threading
import subprocess
from datetime import datetime
from typing import Dict
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="LiteNVR for ZimaOS")

STORAGE_DIR = "/storage"
CONFIG_PATH = os.path.join(STORAGE_DIR, "config.json")
RECORDINGS_DIR = os.path.join(STORAGE_DIR, "recordings")
SNAPSHOTS_DIR = os.path.join(STORAGE_DIR, "snapshots")
DB_PATH = os.path.join(STORAGE_DIR, "nvr.db")
HLS_DIR = "/dev/shm/hls"

os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
os.makedirs(HLS_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "retention_days": 7,
    "cameras": {}
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

CONFIG = load_config()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cam_id TEXT,
            timestamp INTEGER,
            date_str TEXT,
            time_str TEXT,
            label TEXT,
            snapshot TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cam_date ON events(cam_id, date_str)")
    conn.commit()
    conn.close()

init_db()

ffmpeg_processes: Dict[str, subprocess.Popen] = {}
worker_flags: Dict[str, bool] = {}

def start_camera_worker(cam_id: str):
    worker_flags[cam_id] = True
    while worker_flags.get(cam_id, False):
        cam_info = CONFIG.get("cameras", {}).get(cam_id)
        if not cam_info:
            break
        rtsp_url = cam_info["rtsp_url"]
        cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
        cam_hls_dir = os.path.join(HLS_DIR, cam_id)
        os.makedirs(cam_rec_dir, exist_ok=True)
        os.makedirs(cam_hls_dir, exist_ok=True)

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "copy", "-c:a", "aac",
            "-f", "segment",
            "-segment_time", "60",
            "-reset_timestamps", "1",
            "-strftime", "1",
            os.path.join(cam_rec_dir, "%Y-%m-%d_%H-%M-%S.mp4"),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            os.path.join(cam_hls_dir, "live.m3u8")
        ]

        proc = subprocess.Popen(cmd)
        ffmpeg_processes[cam_id] = proc
        proc.wait()
        if worker_flags.get(cam_id, False):
            time.sleep(5)

def stop_camera_worker(cam_id: str):
    worker_flags[cam_id] = False
    proc = ffmpeg_processes.pop(cam_id, None)
    if proc and proc.poll() is None:
        proc.terminate()

def cleanup_worker():
    while True:
        retention_days = CONFIG.get("retention_days", 7)
        cutoff = time.time() - (retention_days * 86400)
        for root, _, files in os.walk(RECORDINGS_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                try:
                    if os.path.getmtime(filepath) < cutoff:
                        os.remove(filepath)
                except Exception:
                    pass
        time.sleep(3600)

@app.on_event("startup")
def on_startup():
    for cam_id in CONFIG.get("cameras", {}):
        threading.Thread(target=start_camera_worker, args=(cam_id,), daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()

class CameraItem(BaseModel):
    cam_id: str
    name: str
    rtsp_url: str

class MotionPayload(BaseModel):
    cam_id: str
    label: str = "Chuyển động (HASS)"

@app.get("/api/system")
def get_system_info():
    total, used, free = shutil.disk_usage(STORAGE_DIR)
    return {
        "retention_days": CONFIG.get("retention_days", 7),
        "disk_total_gb": round(total / (1024**3), 2),
        "disk_used_gb": round(used / (1024**3), 2),
        "disk_free_gb": round(free / (1024**3), 2),
        "disk_percent": round((used / total) * 100, 1) if total else 0
    }

@app.get("/api/cameras")
def get_cameras():
    return CONFIG.get("cameras", {})

@app.post("/api/cameras")
def add_or_update_camera(item: CameraItem):
    cam_id = item.cam_id.strip()
    if not cam_id:
        raise HTTPException(status_code=400, detail="Mã camera không hợp lệ")
    if cam_id in ffmpeg_processes:
        stop_camera_worker(cam_id)
    CONFIG.setdefault("cameras", {})[cam_id] = {
        "name": item.name,
        "rtsp_url": item.rtsp_url
    }
    save_config(CONFIG)
    threading.Thread(target=start_camera_worker, args=(cam_id,), daemon=True).start()
    return {"status": "ok", "cameras": CONFIG["cameras"]}

@app.delete("/api/cameras/{cam_id}")
def delete_camera(cam_id: str):
    if cam_id in CONFIG.get("cameras", {}):
        stop_camera_worker(cam_id)
        del CONFIG["cameras"][cam_id]
        save_config(CONFIG)
    return {"status": "ok"}

@app.post("/api/motion")
def trigger_motion(payload: MotionPayload):
    if payload.cam_id not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")

    now = datetime.now()
    ts = int(now.timestamp())
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    snap_filename = f"{payload.cam_id}_{ts}.jpg"
    snap_path = os.path.join(SNAPSHOTS_DIR, snap_filename)

    rtsp_url = CONFIG["cameras"][payload.cam_id]["rtsp_url"]
    subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", rtsp_url,
        "-frames:v", "1", "-q:v", "3", snap_path
    ])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (payload.cam_id, ts, date_str, time_str, payload.label, snap_filename)
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "timestamp": ts, "snapshot": snap_filename}

@app.get("/api/timeline/{cam_id}")
def get_timeline(cam_id: str, date: str):
    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    segments = []
    if os.path.exists(cam_rec_dir):
        for fname in sorted(os.listdir(cam_rec_dir)):
            if fname.startswith(date) and fname.endswith(".mp4"):
                try:
                    time_part = fname.replace(".mp4", "").split("_")[1]
                    h, m, s = map(int, time_part.split("-"))
                    start_sec = h * 3600 + m * 60 + s
                    segments.append({
                        "file": f"/recordings/{cam_id}/{fname}",
                        "time": time_part.replace("-", ":"),
                        "start_sec": start_sec
                    })
                except Exception:
                    pass

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, timestamp, time_str, label, snapshot FROM events WHERE cam_id=? AND date_str=? ORDER BY timestamp DESC",
        (cam_id, date)
    )
    events = []
    for row in c.fetchall():
        h, m, s = map(int, row[2].split(":"))
        events.append({
            "id": row[0],
            "timestamp": row[1],
            "time": row[2],
            "sec_of_day": h * 3600 + m * 60 + s,
            "label": row[3],
            "snapshot": f"/snapshots/{row[4]}"
        })
    conn.close()
    return {"segments": segments, "events": events}

app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")
app.mount("/snapshots", StaticFiles(directory=SNAPSHOTS_DIR), name="snapshots")
app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")
