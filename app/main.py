import os
import json
import time
import base64
import shutil
import sqlite3
import secrets
import mimetypes
import threading
import subprocess
import urllib.request
import urllib.error
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

# Đảm bảo trình duyệt nhận đúng MIME cho HLS (nếu thiếu, live view không phát được)
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")

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
    threading.Thread(target=hass_motion_listener, daemon=True).start()
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

@app.middleware("http")
async def hls_no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/hls/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

DEFAULT_CONFIG = {
    "retention_days": 7,
    "max_storage_gb": 0,       # 0 = Không giới hạn theo GB
    "max_disk_percent": 85,    # Tự xóa file cũ nhất nếu ổ cứng đầy quá 85%
    "post_motion_sec": 60,     # Thời gian ghi tiếp sau khi có chuyển động (giây)
    "cameras": {},
    "homeassistant": {
        "enabled": False,
        "base_url": "",       # VD: http://homeassistant.local:8123
        "token": "",          # Long-Lived Access Token của Home Assistant
        "entities": {}        # cam_id -> [entity_id cảm biến chuyển động, ...]
    }
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            ha = cfg.setdefault("homeassistant", {})
            for k, v in DEFAULT_CONFIG["homeassistant"].items():
                ha.setdefault(k, v)
            # Di trú: camera cũ lưu "copy" nay chuyển sang "auto" để tự dò H.265
            for cam in cfg.get("cameras", {}).values():
                if cam.get("video_codec") in (None, "", "copy"):
                    cam["video_codec"] = "auto"
            return cfg
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))

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
# Chẩn đoán trạng thái camera (cho UI/log)
cam_status: Dict[str, dict] = {}
# Cache codec đã dò được: cam_id -> (url_key, codec_name)
codec_cache: Dict[str, tuple] = {}

def detect_video_codec(cam_id: str, cam_info: dict):
    """Dò codec video thật của luồng RTSP bằng ffprobe (chỉ cache khi thành công)."""
    url = cam_info.get("rtsp_url")
    key = (url, cam_info.get("sub_rtsp_url"))
    cached = codec_cache.get(cam_id)
    if cached and cached[0] == key:
        return cached[1]
    cmd = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", cam_info.get("rtsp_transport", "tcp"),
        "-timeout", "8000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=nw=1:nk=1",
        url,
    ]
    codec = ""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
        codec = (out or "").strip().splitlines()[0].strip().lower() if out.strip() else ""
    except Exception:
        codec = ""
    if codec:
        codec_cache[cam_id] = (key, codec)
    return codec

def resolve_video_codec(cam_id: str, cam_info: dict):
    """Trả về 'copy' hoặc 'h264'. Chế độ 'auto' dò codec thật của camera."""
    requested = cam_info.get("video_codec", "auto")
    if requested in ("copy", "h264"):
        return requested
    codec = detect_video_codec(cam_id, cam_info)
    cam_status.setdefault(cam_id, {})["detected_codec"] = codec
    if not codec:
        return "copy"          # không dò được -> giữ nguyên (rẻ CPU)
    return "copy" if codec in ("h264", "avc1", "h264_vaapi", "avc") else "h264"

def build_ffmpeg_cmd(cam_id: str, cam_info: dict, video_codec: Optional[str] = None):
    main_url = cam_info["rtsp_url"]
    sub_url = cam_info.get("sub_rtsp_url") or main_url
    transport = cam_info.get("rtsp_transport", "tcp")
    audio_mode = cam_info.get("audio_mode", "aac")  # aac | none | copy
    record_mode = cam_info.get("record_mode", "continuous")  # continuous | motion | off
    explicit = cam_info.get("video_codec", "auto")
    if video_codec is None:
        video_codec = resolve_video_codec(cam_id, cam_info)  # copy | h264 cho LIVE
    # Bản ghi lưu NGUYÊN codec gốc để nhẹ CPU (kể cả HEVC). Khi xem lại sẽ tự
    # chuyển mã. Chỉ ép H.264 khi người dùng chọn thủ công "h264".
    record_codec = "h264" if explicit == "h264" else "copy"

    # H.264 transcode cho LUỒNG LIVE khi camera dùng H.265/HEVC (trình duyệt không phát được HEVC)
    vcodec_args = (
        ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
         "-g", "50", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]
        if video_codec == "h264" else ["-c:v", "copy"]
    )

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
        if record_codec == "h264":
            cmd.extend(vcodec_args)
            if audio_mode == "none":
                cmd.extend(["-an"])
            else:
                cmd.extend(["-map", "0:a?", "-c:a", "aac"])
        elif audio_mode == "aac":
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
        *vcodec_args, "-an",
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
        st = cam_status.setdefault(cam_id, {})
        st["log"] = st.get("log", deque(maxlen=20))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as e:
            st["running"] = False
            st["last_error"] = str(e)
            print(f"[LiteNVR] ffmpeg lỗi khởi động cho {cam_id}: {e}", flush=True)
            if token.wait(backoff):
                break
            backoff = min(backoff * 2, 60)
            continue

        ffmpeg_processes[cam_id] = proc
        st["running"] = True
        st["last_start"] = int(started)

        def _drain(p, cid, status):
            try:
                for line in p.stderr:
                    line = line.strip()
                    if line:
                        status["log"].append(line)
                        status["last_error"] = line
            except Exception:
                pass

        threading.Thread(target=_drain, args=(proc, cam_id, st), daemon=True).start()
        proc.wait()
        st["running"] = False
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
    video_codec: str = "auto"        # auto | copy | h264 (h264 = tương thích trình duyệt)

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
        "record_mode": item.record_mode,
        "video_codec": item.video_codec
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

def handle_motion(cam_id: str, label: str):
    """Ghi nhận 1 sự kiện chuyển động: kích hoạt ghi hình + chụp snapshot + lưu event."""
    cam_info = CONFIG.get("cameras", {}).get(cam_id)
    if not cam_info:
        return None

    now = datetime.now()
    ts = int(now.timestamp())
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    snap_filename = f"{cam_id}_{ts}.jpg"
    snap_path = os.path.join(SNAPSHOTS_DIR, snap_filename)

    # Kích hoạt cờ lưu video từ RAM xuống ổ cứng nếu camera đang ở chế độ Motion Only
    post_sec = int(CONFIG.get("post_motion_sec", 60))
    motion_active_until[cam_id] = time.time() + post_sec

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
        print(f"[LiteNVR] Không chụp được snapshot cho {cam_id}: {e}", flush=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (cam_id, ts, date_str, time_str, label, snap_filename)
    )
    conn.commit()
    conn.close()
    return {
        "status": "ok",
        "timestamp": ts,
        "record_mode": cam_info.get("record_mode", "continuous"),
        "recording_until": motion_active_until[cam_id]
    }

@app.post("/api/motion")
def trigger_motion(payload: MotionPayload):
    result = handle_motion(payload.cam_id, payload.label)
    if result is None:
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    return result

# Cấu hình liên kết Home Assistant
class HassRule(BaseModel):
    entity_id: str
    label: str = ""            # Nhãn sự kiện, VD: "Người quen", "Người lạ", tên người

class HassSettings(BaseModel):
    enabled: bool = False
    base_url: str = ""
    token: str = ""            # Để trống khi lưu = giữ token cũ
    entities: Dict[str, List[HassRule]] = {}

def _hass_rules(entities: Dict[str, Any]) -> Dict[str, tuple]:
    """Chuẩn hoá cấu hình entity -> bảng tra entity_id -> (cam_id, label).
    Hỗ trợ cả định dạng cũ (list chuỗi) và mới (list {entity_id, label})."""
    watch: Dict[str, tuple] = {}
    for cam_id, ent_list in (entities or {}).items():
        for ent in (ent_list or []):
            if isinstance(ent, dict):
                eid = (ent.get("entity_id") or "").strip()
                label = (ent.get("label") or "").strip()
            else:
                eid = str(ent).strip()
                label = ""
            if eid:
                watch[eid] = (cam_id, label)
    return watch

# Trạng thái kết nối HA để hiển thị lên UI
hass_state = {"connected": False, "last_error": "", "last_event": "", "last_event_at": 0}

def _hass_default_headers(token: str):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def hass_ping(base_url: str, token: str):
    """Gọi REST /api/ để kiểm tra URL + token HA."""
    url = base_url.strip().rstrip("/") + "/api/"
    req = urllib.request.Request(url, headers=_hass_default_headers(token))
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))

def _hass_ws_url(base_url: str):
    base = base_url.strip().rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not (base.startswith("ws://") or base.startswith("wss://")):
        base = "ws://" + base
    return base + "/api/websocket"

MOTION_STATES = ("on", "detected", "motion", "true", "active")

def hass_motion_listener():
    """Kết nối WebSocket tới HA, lắng nghe state_changed của các entity đã map
    và kích hoạt ghi hình khi cảm biến chuyển sang trạng thái phát hiện."""
    try:
        import websocket  # websocket-client
    except Exception as e:
        print(f"[LiteNVR] Thiếu thư viện websocket-client, tắt liên kết HA: {e}", flush=True)
        return

    ws = None
    last_sig = None
    backoff = 5

    def close_ws():
        nonlocal ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            ws = None

    while True:
        ha = CONFIG.get("homeassistant", {}) or {}
        enabled = bool(ha.get("enabled"))
        base_url = (ha.get("base_url") or "").strip()
        token = (ha.get("token") or "").strip()
        entities: Dict[str, list] = ha.get("entities") or {}

        if not enabled or not base_url or not token:
            close_ws()
            last_sig = None
            hass_state["connected"] = False
            time.sleep(3)
            continue

        # Bảng tra: entity_id -> (cam_id, label)
        watch = _hass_rules(entities)

        sig = (base_url, token, tuple(sorted(watch.items())))
        if ws is not None and sig != last_sig:
            close_ws()

        if ws is None:
            try:
                ws = websocket.create_connection(_hass_ws_url(base_url), timeout=15)
                ws.recv()  # auth_required
                ws.send(json.dumps({"type": "auth", "access_token": token}))
                resp = json.loads(ws.recv())
                if resp.get("type") != "auth_ok":
                    raise RuntimeError(f"Xác thực HA thất bại: {resp.get('type')}")
                ws.send(json.dumps({
                    "id": 1, "type": "subscribe_events",
                    "event_type": "state_changed"
                }))
                ws.recv()  # ack subscribe
                last_sig = sig
                backoff = 5
                hass_state["connected"] = True
                hass_state["last_error"] = ""
                print("[LiteNVR] Đã kết nối Home Assistant, đang lắng nghe chuyển động.", flush=True)
            except Exception as e:
                hass_state["connected"] = False
                hass_state["last_error"] = str(e)
                print(f"[LiteNVR] Lỗi kết nối HA: {e}", flush=True)
                close_ws()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

        try:
            ws.settimeout(1)
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            hass_state["connected"] = False
            hass_state["last_error"] = str(e)
            print(f"[LiteNVR] Mất kết nối HA, thử lại: {e}", flush=True)
            close_ws()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("type") != "event":
            continue
        event = msg.get("event") or {}
        if event.get("event_type") != "state_changed":
            continue
        data = event.get("data") or {}
        entity_id = str(data.get("entity_id") or "")
        new_state = data.get("new_state") or {}
        prev_state = (data.get("old_state") or {}).get("state")
        state = str(new_state.get("state", "")).lower()
        # Chỉ kích hoạt khi chuyển TỪ trạng thái khác SANG trạng thái phát hiện
        if state == str(prev_state).lower():
            continue
        rule = watch.get(entity_id)
        if not rule or state not in MOTION_STATES:
            continue
        cam_id, custom_label = rule
        label = custom_label or f"Chuyển động (HA: {entity_id})"
        try:
            handle_motion(cam_id, label)
            hass_state["last_event"] = f"{cam_id} <- {entity_id} ({state})"
            hass_state["last_event_at"] = int(time.time())
            print(f"[LiteNVR] HA motion: {hass_state['last_event']}", flush=True)
        except Exception as e:
            print(f"[LiteNVR] Lỗi xử lý HA motion: {e}", flush=True)

@app.get("/api/hass")
def get_hass_settings():
    ha = CONFIG.get("homeassistant", {}) or {}
    watch = _hass_rules(ha.get("entities") or {})
    rules: Dict[str, List[dict]] = {}
    for eid, (cam_id, label) in watch.items():
        rules.setdefault(cam_id, []).append({"entity_id": eid, "label": label})
    return {
        "enabled": bool(ha.get("enabled")),
        "base_url": ha.get("base_url", ""),
        "has_token": bool((ha.get("token") or "").strip()),
        "entities": rules,
        "status": hass_state,
    }

@app.post("/api/hass")
def update_hass_settings(settings: HassSettings):
    ha = CONFIG.setdefault("homeassistant", {})
    ha["enabled"] = settings.enabled
    ha["base_url"] = settings.base_url.strip()
    if settings.token.strip():
        ha["token"] = settings.token.strip()
    ha["entities"] = {
        cam_id: [
            {"entity_id": r.entity_id.strip(), "label": r.label.strip()}
            for r in (rules or []) if r.entity_id.strip()
        ]
        for cam_id, rules in (settings.entities or {}).items()
    }
    save_config(CONFIG)
    return {"status": "ok"}

@app.post("/api/hass/entities")
def hass_available_entities(settings: HassSettings):
    """Trả danh sách cảm biến/entity từ Home Assistant để UI gợi ý chọn."""
    base_url = settings.base_url.strip() or (CONFIG.get("homeassistant", {}).get("base_url") or "").strip()
    token = settings.token.strip() or (CONFIG.get("homeassistant", {}).get("token") or "").strip()
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="Chưa cấu hình URL hoặc Token của Home Assistant")
    url = base_url.rstrip("/") + "/api/states"
    req = urllib.request.Request(url, headers=_hass_default_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            states = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HA trả về lỗi HTTP {e.code} (sai token?)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không lấy được danh sách sensor: {e}")

    out = []
    for s in states:
        eid = str(s.get("entity_id", ""))
        if not (eid.startswith("binary_sensor.") or eid.startswith("input_boolean.")
                or eid.startswith("switch.") or eid.startswith("device_tracker.")):
            continue
        attrs = s.get("attributes") or {}
        out.append({
            "entity_id": eid,
            "name": attrs.get("friendly_name", eid),
            "device_class": attrs.get("device_class", "") or "",
            "state": s.get("state", ""),
        })
    out.sort(key=lambda x: ((x["device_class"] or "zzz"), x["name"].lower()))
    return {"entities": out}

@app.post("/api/hass/test")
def test_hass_connection(settings: HassSettings):
    token = settings.token.strip() or (CONFIG.get("homeassistant", {}).get("token") or "").strip()
    base_url = settings.base_url.strip() or (CONFIG.get("homeassistant", {}).get("base_url") or "").strip()
    if not base_url or not token:
        raise HTTPException(status_code=400, detail="Thiếu URL hoặc Token của Home Assistant")
    try:
        info = hass_ping(base_url, token)
        return {"status": "ok", "message": info.get("message", "API running")}
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"HA trả về lỗi HTTP {e.code} (sai token?)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không kết nối được HA: {e}")

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

@app.get("/api/status/{cam_id}")
def get_cam_status(cam_id: str):
    hls_path = os.path.join(HLS_DIR, cam_id, "live.m3u8")
    hls_ready = os.path.exists(hls_path) and (time.time() - os.path.getmtime(hls_path) < 15)
    st = cam_status.get(cam_id, {})
    return {
        "running": bool(st.get("running")),
        "hls_ready": hls_ready,
        "detected_codec": st.get("detected_codec", ""),
        "last_error": st.get("last_error", ""),
        "log": list(st.get("log", [])),
    }

@app.get("/api/play/{cam_id}/{fname}")
def play_recording(cam_id: str, fname: str, t: int = 0):
    """Phát bản ghi. t=1: transcode sang H.264/AAC để trình duyệt phát được
    (dùng cho camera H.265/HEVC mà không cần ghi lại)."""
    safe_cam = os.path.basename(cam_id)
    safe_name = os.path.basename(fname)
    path = os.path.join(RECORDINGS_DIR, safe_cam, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")

    if not t:
        return FileResponse(path, media_type="video/mp4")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = proc.stdout
    if out is None:
        proc.terminate()
        raise HTTPException(status_code=500, detail="Không mở được luồng ffmpeg")

    def _stream():
        try:
            while True:
                chunk = out.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                out.close()
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()

    return StreamingResponse(_stream(), media_type="video/mp4")

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
