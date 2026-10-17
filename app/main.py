import os
import json
import time
import base64
import shutil
import sqlite3
import secrets
import threading
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

NVR_USER = os.environ.get("NVR_USER", "admin")
NVR_PASS = os.environ.get("NVR_PASS", "admin")
AUTH_ENABLED = os.environ.get("NVR_AUTH", "on").lower() not in ("off", "0", "false", "no")

STORAGE_DIR = "/storage"
CONFIG_PATH = os.path.join(STORAGE_DIR, "config.json")
RECORDINGS_DIR = os.path.join(STORAGE_DIR, "recordings")
SNAPSHOTS_DIR = os.path.join(STORAGE_DIR, "snapshots")
DB_PATH = os.path.join(STORAGE_DIR, "nvr.db")
HLS_DIR = "/dev/shm/hls"
BUFFER_DIR = "/dev/shm/buffer"

for d in [RECORDINGS_DIR, SNAPSHOTS_DIR, HLS_DIR, BUFFER_DIR]:
    os.makedirs(d, exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    for cam_id in list(CONFIG.get("cameras", {})):
        start_camera_worker(cam_id)
    threading.Thread(target=motion_buffer_manager, daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()
    yield

app = FastAPI(title="LiteNVR Pro for ZimaOS", lifespan=lifespan)

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not AUTH_ENABLED or request.url.path == "/health":
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            if secrets.compare_digest(user, NVR_USER) and secrets.compare_digest(pw, NVR_PASS):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="LiteNVR"'},
        content="Unauthorized",
    )

DEFAULT_CONFIG = {
    "retention_days": 7,
    "max_storage_gb": 0,       # 0 = Không giới hạn theo GB
    "max_disk_percent": 85,    # Tự xóa file cũ nhất nếu ổ cứng đầy quá 85%
    "post_motion_sec": 60,     # Thời gian ghi tiếp sau khi có chuyển động (giây)
    "cameras": {}
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
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
worker_tokens: Dict[str, threading.Event] = {}
worker_lock = threading.Lock()
# Lưu thời điểm kết thúc ghi hình chuyển động của từng camera (Unix timestamp)
motion_active_until: Dict[str, float] = {}

def build_ffmpeg_cmd(cam_id: str, cam_info: dict):
    main_url = cam_info["rtsp_url"]
    sub_url = cam_info.get("sub_rtsp_url") or main_url
    transport = cam_info.get("rtsp_transport", "tcp")
    audio_mode = cam_info.get("audio_mode", "aac")  # aac | none | copy
    record_mode = cam_info.get("record_mode", "continuous")  # continuous | motion | off

    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    cam_buf_dir = os.path.join(BUFFER_DIR, cam_id)
    cam_hls_dir = os.path.join(HLS_DIR, cam_id)
    for d in [cam_rec_dir, cam_buf_dir, cam_hls_dir]:
        os.makedirs(d, exist_ok=True)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-rtsp_transport", transport,
        "-timeout", "10000000",
        "-i", main_url
    ]

    # Nếu có luồng phụ riêng cho Live View thì thêm input số 1
    use_dual_input = (sub_url != main_url)
    if use_dual_input:
        cmd.extend([
            "-fflags", "+genpts+igndts+discardcorrupt",
            "-rtsp_transport", transport,
            "-timeout", "10000000",
            "-i", sub_url
        ])

    # Nhánh 1: Ghi hình (Continuous lưu thẳng HDD, Motion lưu tạm vào RAM buffer 15s/đoạn)
    if record_mode in ("continuous", "motion"):
        cmd.extend(["-map", "0:v:0"])
        if audio_mode == "aac":
            cmd.extend(["-map", "0:a?", "-c:v", "copy", "-c:a", "aac"])
        elif audio_mode == "copy":
            cmd.extend(["-map", "0:a?", "-c:v", "copy", "-c:a", "copy"])
        else:
            cmd.extend(["-c:v", "copy", "-an"])

        seg_time = "60" if record_mode == "continuous" else "15"
        target_dir = cam_rec_dir if record_mode == "continuous" else cam_buf_dir

        cmd.extend([
            "-f", "segment",
            "-segment_time", seg_time,
            "-reset_timestamps", "1",
            "-strftime", "1",
            os.path.join(target_dir, "%Y-%m-%d_%H-%M-%S.mp4")
        ])

    # Nhánh 2: Phát trực tiếp HLS trên RAM (/dev/shm/hls)
    live_input_idx = "1:v:0" if use_dual_input else "0:v:0"
    cmd.extend([
        "-map", live_input_idx,
        "-c:v", "copy", "-an",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        os.path.join(cam_hls_dir, "live.m3u8")
    ])
    return cmd

def start_camera_worker(cam_id: str):
    with worker_lock:
        old = worker_tokens.get(cam_id)
        if old:
            old.set()
        token = threading.Event()
        worker_tokens[cam_id] = token
    threading.Thread(target=_camera_worker_loop, args=(cam_id, token), daemon=True).start()

def _camera_worker_loop(cam_id: str, token: threading.Event):
    backoff = 5
    while not token.is_set():
        cam_info = CONFIG.get("cameras", {}).get(cam_id)
        if not cam_info:
            break
        cmd = build_ffmpeg_cmd(cam_id, cam_info)
        started = time.time()
        try:
            proc = subprocess.Popen(cmd)
        except Exception as e:
            print(f"[LiteNVR] ffmpeg lỗi khởi động cho {cam_id}: {e}", flush=True)
            if token.wait(backoff):
                break
            backoff = min(backoff * 2, 60)
            continue
        ffmpeg_processes[cam_id] = proc
        proc.wait()
        if ffmpeg_processes.get(cam_id) is proc:
            ffmpeg_processes.pop(cam_id, None)
        if token.is_set():
            break
        # Backoff: cam offline liên tục thì giãn dần, tránh spam ffmpeg
        ran = time.time() - started
        backoff = 5 if ran > 10 else min(backoff * 2, 60)
        if token.wait(backoff):
            break

def stop_camera_worker(cam_id: str):
    with worker_lock:
        token = worker_tokens.pop(cam_id, None)
    if token:
        token.set()
    proc = ffmpeg_processes.get(cam_id)
    if proc and proc.poll() is None:
        proc.terminate()

# Luồng quản lý bộ đệm RAM cho chế độ "Chỉ lưu khi có chuyển động (motion)"
def motion_buffer_manager():
    while True:
        now = time.time()
        for cam_id, cam_info in list(CONFIG.get("cameras", {}).items()):
            if cam_info.get("record_mode") != "motion":
                continue
            cam_buf_dir = os.path.join(BUFFER_DIR, cam_id)
            cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
            if not os.path.exists(cam_buf_dir):
                continue

            files = sorted(
                [
                    os.path.join(cam_buf_dir, f)
                    for f in os.listdir(cam_buf_dir) if f.endswith(".mp4")
                ],
                key=os.path.getmtime,
            )
            # Bỏ qua file cuối cùng vì FFmpeg đang ghi dở
            completed_files = files[:-1] if len(files) > 1 else []
            is_motion_active = now <= motion_active_until.get(cam_id, 0)

            for fpath in completed_files:
                fname = os.path.basename(fpath)
                mtime = os.path.getmtime(fpath)
                if is_motion_active:
                    # Đang có chuyển động -> Di chuyển cả đoạn đệm trước đó xuống ổ cứng HDD
                    shutil.move(fpath, os.path.join(cam_rec_dir, fname))
                else:
                    # Không có chuyển động -> Chỉ giữ lại 3 đoạn gần nhất (~45 giây pre-roll) trong RAM, xóa đoạn cũ
                    if now - mtime > 45:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
        time.sleep(2)

# Hàm tính tổng dung lượng thư mục lưu trữ (Bytes), có cache để tránh walk ổ cứng liên tục
_size_cache = {"t": 0.0, "v": 0}
def get_storage_dir_size(force: bool = False):
    now = time.time()
    if not force and now - _size_cache["t"] < 60:
        return _size_cache["v"]
    total_size = 0
    for root, _, files in os.walk(STORAGE_DIR):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
            except Exception:
                pass
    _size_cache["t"] = now
    _size_cache["v"] = total_size
    return total_size

# Bộ dọn dẹp ổ cứng thông minh (Theo số ngày + Giới hạn GB + Giới hạn % ổ cứng)
def run_cleanup_now():
    retention_days = float(CONFIG.get("retention_days", 7))
    max_storage_gb = float(CONFIG.get("max_storage_gb", 0))
    max_disk_percent = float(CONFIG.get("max_disk_percent", 85))
    deleted_count = 0

    # 1. Xóa theo số ngày lưu trữ (Retention Days)
    if retention_days > 0:
        cutoff = time.time() - (retention_days * 86400)
        for folder in [RECORDINGS_DIR, SNAPSHOTS_DIR]:
            for root, _, files in os.walk(folder):
                for file in files:
                    filepath = os.path.join(root, file)
                    try:
                        if os.path.getmtime(filepath) < cutoff:
                            os.remove(filepath)
                            deleted_count += 1
                    except Exception:
                        pass

    # 2. Xóa cuốn chiếu file cũ nhất nếu vượt quá giới hạn GB hoặc % ổ cứng
    all_media = []
    for folder in [RECORDINGS_DIR, SNAPSHOTS_DIR]:
        for root, _, files in os.walk(folder):
            for file in files:
                fp = os.path.join(root, file)
                try:
                    all_media.append((os.path.getmtime(fp), os.path.getsize(fp), fp))
                except Exception:
                    pass

    all_media.sort(key=lambda x: x[0])  # Sắp xếp từ cũ nhất đến mới nhất
    media_bytes = sum(size for _, size, _ in all_media)
    max_bytes = max_storage_gb * (1024 ** 3) if max_storage_gb > 0 else 0

    for mtime, fsize, fp in all_media:
        total, used, _ = shutil.disk_usage(STORAGE_DIR)
        disk_pct = (used / total) * 100 if total else 0
        over_gb = (max_bytes > 0 and media_bytes > max_bytes)
        over_pct = (max_disk_percent > 0 and disk_pct > max_disk_percent)

        if not (over_gb or over_pct):
            break
        try:
            os.remove(fp)
            media_bytes -= fsize
            deleted_count += 1
        except Exception:
            pass

    _size_cache["t"] = 0.0
    return deleted_count

def cleanup_worker():
    while True:
        try:
            run_cleanup_now()
        except Exception:
            pass
        time.sleep(300)  # Kiểm tra dọn ổ cứng mỗi 5 phút

class CameraItem(BaseModel):
    cam_id: str
    name: str
    brand: str = "generic"
    rtsp_url: str
    sub_rtsp_url: Optional[str] = ""
    rtsp_transport: str = "tcp"
    audio_mode: str = "aac"
    record_mode: str = "continuous"  # continuous | motion | off

class StorageSettings(BaseModel):
    retention_days: float
    max_storage_gb: float
    max_disk_percent: float
    post_motion_sec: int

class MotionPayload(BaseModel):
    cam_id: str
    label: str = "Chuyển động (HASS)"

@app.get("/api/system")
def get_system_info():
    total, used, free = shutil.disk_usage(STORAGE_DIR)
    nvr_used_bytes = get_storage_dir_size()
    return {
        "retention_days": CONFIG.get("retention_days", 7),
        "max_storage_gb": CONFIG.get("max_storage_gb", 0),
        "max_disk_percent": CONFIG.get("max_disk_percent", 85),
        "post_motion_sec": CONFIG.get("post_motion_sec", 60),
        "nvr_used_gb": round(nvr_used_bytes / (1024**3), 2),
        "disk_total_gb": round(total / (1024**3), 2),
        "disk_used_gb": round(used / (1024**3), 2),
        "disk_free_gb": round(free / (1024**3), 2),
        "disk_percent": round((used / total) * 100, 1) if total else 0
    }

@app.post("/api/storage")
def update_storage_settings(settings: StorageSettings):
    CONFIG["retention_days"] = settings.retention_days
    CONFIG["max_storage_gb"] = settings.max_storage_gb
    CONFIG["max_disk_percent"] = settings.max_disk_percent
    CONFIG["post_motion_sec"] = settings.post_motion_sec
    save_config(CONFIG)
    deleted = run_cleanup_now()
    return {"status": "ok", "deleted_files": deleted}

@app.post("/api/storage/clean")
def manual_clean():
    deleted = run_cleanup_now()
    return {"status": "ok", "deleted_files": deleted}

@app.get("/api/cameras")
def get_cameras():
    return CONFIG.get("cameras", {})

@app.post("/api/cameras")
def add_or_update_camera(item: CameraItem):
    cam_id = item.cam_id.strip()
    if not cam_id:
        raise HTTPException(status_code=400, detail="Mã camera không hợp lệ")
    stop_camera_worker(cam_id)
    CONFIG.setdefault("cameras", {})[cam_id] = {
        "name": item.name,
        "brand": item.brand,
        "rtsp_url": item.rtsp_url.strip(),
        "sub_rtsp_url": (item.sub_rtsp_url or "").strip(),
        "rtsp_transport": item.rtsp_transport,
        "audio_mode": item.audio_mode,
        "record_mode": item.record_mode
    }
    save_config(CONFIG)
    start_camera_worker(cam_id)
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
    cam_info = CONFIG.get("cameras", {}).get(payload.cam_id)
    if not cam_info:
        raise HTTPException(status_code=404, detail="Camera không tồn tại")

    now = datetime.now()
    ts = int(now.timestamp())
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    snap_filename = f"{payload.cam_id}_{ts}.jpg"
    snap_path = os.path.join(SNAPSHOTS_DIR, snap_filename)

    # Kích hoạt cờ lưu video từ RAM xuống ổ cứng nếu camera đang ở chế độ Motion Only
    post_sec = int(CONFIG.get("post_motion_sec", 60))
    motion_active_until[payload.cam_id] = time.time() + post_sec

    # Ưu tiên chụp Snapshot từ luồng phụ để lấy ảnh cực nhanh
    snap_rtsp = cam_info.get("sub_rtsp_url") or cam_info["rtsp_url"]
    transport = cam_info.get("rtsp_transport", "tcp")
    try:
        subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", transport, "-i", snap_rtsp,
            "-frames:v", "1", "-q:v", "3", snap_path
        ])
    except Exception as e:
        print(f"[LiteNVR] Không chụp được snapshot cho {payload.cam_id}: {e}", flush=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (payload.cam_id, ts, date_str, time_str, payload.label, snap_filename)
    )
    conn.commit()
    conn.close()
    return {
        "status": "ok",
        "timestamp": ts,
        "record_mode": cam_info.get("record_mode", "continuous"),
        "recording_until": motion_active_until[payload.cam_id]
    }

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

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def index():
    return FileResponse("static/index.html")
