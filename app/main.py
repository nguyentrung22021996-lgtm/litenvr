import os
import json
import time
import base64
import shutil
import socket
import hashlib
import sqlite3
import secrets
import mimetypes
import threading
import subprocess
import urllib.request
import urllib.error
import uuid
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
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

# ---- Giám sát tài nguyên (CPU / RAM / ổ đĩa) cho dashboard ----
_cpu_prev = {"idle": 0, "total": 0, "init": False}
stats_history: deque = deque(maxlen=360)   # ~30 phút nếu lấy mẫu mỗi 5s

def read_cpu_percent() -> float:
    try:
        with open("/proc/stat", "r") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
    except Exception:
        return 0.0
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    if not _cpu_prev["init"]:
        _cpu_prev.update(idle=idle, total=total, init=True)
        return 0.0
    didle = idle - _cpu_prev["idle"]
    dtotal = total - _cpu_prev["total"]
    _cpu_prev.update(idle=idle, total=total)
    return round(100.0 * (1 - didle / dtotal), 1) if dtotal > 0 else 0.0

def read_mem() -> dict:
    info = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
    except Exception:
        return {"mem_total_mb": 0.0, "mem_used_mb": 0.0, "mem_percent": 0.0}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    return {
        "mem_total_mb": round(total / 1024, 1),
        "mem_used_mb": round(used / 1024, 1),
        "mem_percent": round(used / total * 100, 1) if total else 0.0,
    }

def read_disk() -> dict:
    try:
        s = os.statvfs(STORAGE_DIR)
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        used = total - free
        return {
            "disk_total_gb": round(total / 1e9, 2),
            "disk_used_gb": round(used / 1e9, 2),
            "disk_free_gb": round(free / 1e9, 2),
            "disk_percent": round(used / total * 100, 1) if total else 0.0,
        }
    except Exception:
        return {"disk_total_gb": 0.0, "disk_used_gb": 0.0, "disk_free_gb": 0.0, "disk_percent": 0.0}

_ff_cpu_prev: Dict[str, tuple] = {}
def read_cam_cpu() -> Dict[str, float]:
    """%CPU của tiến trình ffmpeg từng camera (đọc /proc/<pid>/stat)."""
    out: Dict[str, float] = {}
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except Exception:
        ticks = 100
    now = time.time()
    for cam_id, proc in list(ffmpeg_processes.items()):
        try:
            with open(f"/proc/{proc.pid}/stat", "r") as f:
                parts = f.read().split()
            cpu_ticks = int(parts[13]) + int(parts[14])
        except Exception:
            continue
        prev = _ff_cpu_prev.get(cam_id)
        _ff_cpu_prev[cam_id] = (now, cpu_ticks)
        if prev:
            dt = now - prev[0]
            dcpu = (cpu_ticks - prev[1]) / ticks
            out[cam_id] = round(100.0 * dcpu / dt, 1) if dt > 0 else 0.0
    return out

def collect_stats() -> dict:
    s = {"t": int(time.time()), "cpu_percent": read_cpu_percent(), "cam_cpu": read_cam_cpu()}
    s.update(read_mem())
    s.update(read_disk())
    return s

def stats_sampler():
    # Lấy mẫu đầu để có baseline CPU (lần đầu luôn trả 0)
    collect_stats()
    while True:
        stats_history.append(collect_stats())
        time.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    for cam_id, cam in list(CONFIG.get("cameras", {}).items()):
        if cam.get("record_mode", "continuous") != "off":
            start_camera_worker(cam_id)
    threading.Thread(target=motion_buffer_manager, daemon=True).start()
    threading.Thread(target=cleanup_worker, daemon=True).start()
    threading.Thread(target=hass_motion_listener, daemon=True).start()
    threading.Thread(target=stats_sampler, daemon=True).start()
    threading.Thread(target=live_supervisor, daemon=True).start()
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
    path = request.url.path
    cam_id = None
    if path.startswith("/hls/"):
        parts = path.split("/")
        if len(parts) > 2:
            cam_id = parts[2]
            ensure_live(cam_id)
    resp = await call_next(request)
    if path.startswith("/hls/"):
        resp.headers["Cache-Control"] = "no-store"
        if resp.status_code == 404:
            # Segment 404 thường gặp khi ffmpeg đang khởi động lại; log thưa để
            # vẫn thấy lỗi thật mà không spam.
            now = time.time()
            key = f"_hls404:{cam_id}"
            st = cam_status.setdefault(cam_id or "?", {})
            if now - st.get(key, 0) > 60:
                st[key] = now
                log_event("warn", f"Không tìm thấy tệp HLS ({path.rsplit('/', 1)[-1]})", cam_id or "")
    return resp

DEFAULT_CONFIG = {
    "retention_days": 7,
    "max_storage_gb": 0,       # 0 = Không giới hạn theo GB
    "max_disk_percent": 85,    # Tự xóa file cũ nhất nếu ổ cứng đầy quá 85%
    "post_motion_sec": 60,     # Thời gian ghi tiếp sau khi có chuyển động (giây)
    "ram_buffer": True,        # True: đệm motion trong RAM; False: ghi đệm ra đĩa
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

_db_local = threading.local()

def db_conn() -> sqlite3.Connection:
    """Kết nối SQLite tái dùng theo luồng, bật WAL để đọc/ghi song song ổn định
    khi nhiều camera cùng ghi sự kiện (tránh 'database is locked')."""
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _db_local.conn = conn
    return conn

def init_db():
    conn = db_conn()
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
    c.execute("CREATE INDEX IF NOT EXISTS idx_cam_ts ON events(cam_id, timestamp)")
    conn.commit()

init_db()

ffmpeg_processes: Dict[str, subprocess.Popen] = {}
worker_tokens: Dict[str, threading.Event] = {}
worker_lock = threading.Lock()
# Camera đang có người xem live (timestamp hoạt động gần nhất)
cam_viewers: Dict[str, float] = {}
viewer_lock = threading.Lock()
LIVE_IDLE_STOP_SEC = int(os.environ.get("LIVE_IDLE_STOP_SEC", "30"))
# Theo dõi cảnh báo đầy ổ (tránh spam log)
_disk_warn = {"t": 0.0}
# Lưu thời điểm kết thúc ghi hình chuyển động của từng camera (Unix timestamp)
motion_active_until: Dict[str, float] = {}
# Chẩn đoán trạng thái camera (cho UI/log)
cam_status: Dict[str, dict] = {}
# Cache codec đã dò được: cam_id -> (url_key, codec_name)
codec_cache: Dict[str, tuple] = {}

# Nhật ký sự kiện toàn ứng dụng (cho tab Log trên UI)
APP_LOG: deque = deque(maxlen=300)
_log_lock = threading.Lock()

def log_event(level: str, msg: str, cam_id: str = ""):
    """Ghi 1 dòng nhật ký (info/warn/error) để UI hiển thị và biết đường sửa."""
    with _log_lock:
        APP_LOG.append({
            "t": int(time.time()),
            "level": level,
            "cam_id": cam_id,
            "msg": msg,
        })

H264_BROWSER_SAFE_PIXFMT = {"yuv420p", "yuvj420p"}

def detect_video_codec(cam_id: str, cam_info: dict):
    """Dò codec + profile + pix_fmt của luồng RTSP (chỉ cache khi thành công).
    Trả về dict {'codec','profile','pix_fmt'} hoặc {} nếu không dò được."""
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
        "-show_entries", "stream=codec_name,profile,pix_fmt",
        "-of", "default=nw=1",
        url,
    ]
    info = {}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout or ""
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().lower()
    except Exception:
        info = {}
    if info.get("codec_name"):
        codec_cache[cam_id] = (key, info)
        return info
    return {}

def _is_browser_safe_h264(codec: str, pix_fmt: str) -> bool:
    """H.264 mà Chrome/Edge/Firefox giải mã được: profile thường + màu 8-bit 4:2:0.
    Các biến thể 4:2:2/4:4:4/10-bit (một số camera Hanet dùng) gây hình rác ('vỡ')."""
    if codec not in ("h264", "avc1", "avc"):
        return False
    if pix_fmt and pix_fmt not in H264_BROWSER_SAFE_PIXFMT:
        return False
    return True

def resolve_video_codec(cam_id: str, cam_info: dict):
    """Trả về 'copy' hoặc 'h264'. Chế độ 'auto' dò codec thật của camera."""
    requested = cam_info.get("video_codec", "auto")
    if requested in ("copy", "h264"):
        return requested
    info = detect_video_codec(cam_id, cam_info)
    codec = info.get("codec_name", "")
    st = cam_status.setdefault(cam_id, {})
    st["detected_codec"] = codec
    st["detected_profile"] = info.get("profile", "")
    st["detected_pix_fmt"] = info.get("pix_fmt", "")
    if not codec:
        # Không dò được (camera bận/đang bị chiếm session, mạng chậm...).
        # Chọn H.264 để LUÔN phát được; nếu camera vốn H.264 thì ffprobe
        # sẽ thành công ở lần chạy sau và tự quay về copy.
        st["detect_failed"] = True
        return "h264"
    st["detect_failed"] = False
    # HEVC, hoặc H.264 ở profile/màu mà trình duyệt không giải mã -> chuyển mã.
    return "copy" if _is_browser_safe_h264(codec, info.get("pix_fmt", "")) else "h264"

# ---------------- ONVIF (tự dò camera & lấy URL RTSP) ----------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def _find_text(root, name: str) -> str:
    for el in root.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            return el.text.strip()
    return ""

def _find_all(root, name: str):
    return [el for el in root.iter() if _local(el.tag) == name]

def _onvif_security(username: str, password: str) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    return (
        '<s:Header><wsse:Security '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f'<wsse:UsernameToken><wsse:Username>{xml_escape(username)}</wsse:Username>'
        f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        f'<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>'
    )

def _onvif_post(url: str, action: str, body: str, username: str = "", password: str = "", timeout: float = 6.0):
    envelope = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'{_onvif_security(username, password)}'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    ).encode("utf-8")
    req = urllib.request.Request(url, data=envelope, method="POST", headers={
        "Content-Type": "application/soap+xml; charset=utf-8",
        "SOAPAction": action,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # Một số camera yêu cầu HTTP Digest cho ONVIF (ngoài WS-Security).
        www = (e.headers.get("WWW-Authenticate") or "").lower()
        if e.code == 401 and username and "digest" in www:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, url, username, password)
            opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
        else:
            raise
    return ET.fromstring(raw)

def onvif_discover(timeout: float = 3.0) -> List[dict]:
    """WS-Discovery qua UDP multicast: tìm camera ONVIF trong mạng LAN."""
    mid = f"uuid:{uuid.uuid4()}"
    msg = (
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header><w:MessageID>{mid}</w:MessageID>'
        '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        '</e:Envelope>'
    ).encode()
    found: Dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(0.5)
        s.sendto(msg, ("239.255.255.250", 3702))
        end = time.time() + max(0.5, min(timeout, 8))
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(data)
            except Exception:
                continue
            addrs = _find_text(root, "XAddrs").split()
            scopes = _find_text(root, "Scopes")
            name = ""
            for part in scopes.split():
                if "name/" in part:
                    name = part.split("name/", 1)[1]
            host = addr[0]
            key = addrs[0] if addrs else host
            found[key] = {
                "ip": host,
                "xaddr": addrs[0] if addrs else "",
                "name": name or host,
                "scopes": scopes,
            }
    finally:
        s.close()
    return sorted(found.values(), key=lambda x: x["ip"])

def _with_rtsp_auth(url: str, user: str, password: str) -> str:
    if not user or "@" in url.split("://", 1)[-1]:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{user}:{password}@{rest}"

def onvif_probe(host: str, port: int = 80, username: str = "", password: str = "") -> dict:
    """Lấy tên thiết bị + URL RTSP main/sub qua ONVIF (GetDeviceInformation/GetProfiles/GetStreamUri)."""
    base = f"http://{host}:{int(port)}"
    device = base + "/onvif/device_service"

    name = ""
    try:
        root = _onvif_post(device, "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                           "<tds:GetDeviceInformation/>", username, password)
        name = _find_text(root, "Manufacturer")
        model = _find_text(root, "Model")
        if model:
            name = (name + " " + model).strip()
    except Exception as e:
        log_event("warn", f"ONVIF GetDeviceInformation lỗi ({host}): {e}")

    media_url = base + "/onvif/media_service"
    def get_profiles(url):
        return _onvif_post(url, "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                           "<trt:GetProfiles/>", username, password)
    try:
        root = get_profiles(media_url)
    except Exception:
        media_url = ""
        try:
            caps = _onvif_post(device, "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
                               "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
                               username, password)
            for el in _find_all(caps, "Media"):
                for child in el.iter():
                    if _local(child.tag) == "XAddr" and child.text:
                        media_url = child.text.strip()
                        break
                if media_url:
                    break
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Không gọi được ONVIF Media: {e}")
        if not media_url:
            raise HTTPException(status_code=502, detail="Camera không cung cấp dịch vụ Media qua ONVIF")
        root = get_profiles(media_url)

    profiles = []
    for prof in _find_all(root, "Profiles"):
        token = prof.attrib.get("token", "")
        pname = ""
        for el in prof.iter():
            if _local(el.tag) == "Name":
                pname = (el.text or "").strip()
                break
        if not token:
            continue
        uri = ""
        try:
            body = (
                "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{xml_escape(token)}</trt:ProfileToken></trt:GetStreamUri>"
            )
            r = _onvif_post(media_url, "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
                            body, username, password)
            uri = _find_text(r, "Uri")
        except Exception as e:
            log_event("warn", f"ONVIF GetStreamUri lỗi profile {token}: {e}")
        profiles.append({"token": token, "name": pname, "uri": _with_rtsp_auth(uri, username, password) if uri else ""})

    usable = [p for p in profiles if p["uri"]]
    def pick(keys):
        for p in usable:
            low = (p["name"] + " " + p["token"]).lower()
            if any(k in low for k in keys):
                return p
        return None
    main = pick(("main", "primary", "profile_1", "profile1")) or (usable[0] if usable else None)
    sub = pick(("sub", "second", "minor", "profile_2", "profile2"))
    if sub is None and len(usable) > 1:
        sub = next((p for p in usable if p is not main), None)
    return {
        "host": host,
        "name": name or host,
        "profiles": profiles,
        "main_rtsp_url": main["uri"] if main else "",
        "sub_rtsp_url": sub["uri"] if sub else "",
    }

def _is_rtsp(url: str) -> bool:
    return (url or "").lower().startswith(("rtsp://", "rtsps://"))

def _input_args(url: str, cam_info: dict) -> List[str]:
    """Tham số đầu vào ffmpeg tuỳ giao thức. RTSP cần transport/timeout;
    HTTP(MJPEG)/HLS/RTMP thì để ffmpeg tự nhận định dạng."""
    if _is_rtsp(url):
        return [
            "-rtsp_transport", cam_info.get("rtsp_transport", "tcp"),
            "-rtsp_flags", "prefer_tcp",
            "-timeout", "10000000",
        ]
    return []

def buffer_root() -> str:
    """Thư mục đệm cho chế độ Motion. Mặc định RAM (/dev/shm) cho nhanh;
    đặt ram_buffer=false để ghi ra đĩa (RAM nhỏ / ổn định hơn)."""
    return BUFFER_DIR if CONFIG.get("ram_buffer", True) else os.path.join(STORAGE_DIR, "buffer")

def build_ffmpeg_cmd(cam_id: str, cam_info: dict, video_codec: Optional[str] = None, include_live: bool = True):
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
    # ultrafast + 2 thread: giảm mạnh CPU/RAM so với veryfast mà vẫn đủ nét cho live.
    vcodec_args = (
        ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-threads", "2", "-g", "50", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]
        if video_codec == "h264" else ["-c:v", "copy"]
    )

    cam_rec_dir = os.path.join(RECORDINGS_DIR, cam_id)
    cam_buf_dir = os.path.join(buffer_root(), cam_id)
    cam_hls_dir = os.path.join(HLS_DIR, cam_id)
    for d in [cam_rec_dir, cam_buf_dir, cam_hls_dir]:
        os.makedirs(d, exist_ok=True)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts+discardcorrupt",
        *_input_args(main_url, cam_info),
        "-i", main_url
    ]

    # Nếu có luồng phụ riêng cho Live View thì thêm input số 1
    use_dual_input = (sub_url != main_url)
    if use_dual_input:
        cmd.extend([
            "-fflags", "+genpts+igndts+discardcorrupt",
            *_input_args(sub_url, cam_info),
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
    # include_live=False khi không ai xem (tiết kiệm CPU transcode).
    if include_live:
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

def mark_viewer(cam_id: str):
    """Đánh dấu có người đang xem live (heartbeat / tải segment HLS)."""
    with viewer_lock:
        cam_viewers[cam_id] = time.time()

def viewers_active(cam_id: str) -> bool:
    with viewer_lock:
        return (time.time() - cam_viewers.get(cam_id, 0)) <= 15

def _needs_transcode(cam_id: str) -> bool:
    cam = CONFIG.get("cameras", {}).get(cam_id, {})
    vc = cam.get("video_codec", "auto")
    if vc == "copy":
        return False
    if vc == "h264":
        return True
    st = cam_status.get(cam_id, {})
    codec = st.get("detected_codec", "")
    if not codec:
        return True
    return not _is_browser_safe_h264(codec, st.get("detected_pix_fmt", ""))

def start_camera_worker(cam_id: str):
    with worker_lock:
        old = worker_tokens.get(cam_id)
        if old and not old.is_set():
            return  # đang chạy rồi
        if old:
            old.set()
        token = threading.Event()
        worker_tokens[cam_id] = token
    threading.Thread(target=_camera_worker_loop, args=(cam_id, token), daemon=True).start()

def _camera_worker_loop(cam_id: str, token: threading.Event):
    backoff = 2
    st = cam_status.setdefault(cam_id, {})
    st["log"] = st.get("log", deque(maxlen=20))
    while not token.is_set():
        cam_info = CONFIG.get("cameras", {}).get(cam_id)
        if not cam_info:
            break
        record_needed = cam_info.get("record_mode", "continuous") != "off"
        viewers = viewers_active(cam_id)
        if not record_needed and not viewers:
            log_event("info", "Dừng live (không còn người xem)", cam_id)
            break
        resolved = resolve_video_codec(cam_id, cam_info)
        # Khi không ai xem: nếu luồng gốc đã browser-safe thì giữ HLS (rẻ),
        # còn nếu phải transcode thì bỏ nhánh HLS để tiết kiệm CPU.
        include_live = viewers or not _needs_transcode(cam_id)
        st["_live_mode"] = include_live
        cmd = build_ffmpeg_cmd(cam_id, cam_info, video_codec=resolved, include_live=include_live)
        started = time.time()
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as e:
            st["running"] = False
            st["last_error"] = str(e)
            log_event("error", f"ffmpeg không khởi động được: {e}", cam_id)
            print(f"[LiteNVR] ffmpeg lỗi khởi động cho {cam_id}: {e}", flush=True)
            if token.wait(backoff):
                break
            backoff = min(backoff * 2, 60)
            continue

        ffmpeg_processes[cam_id] = proc
        st["running"] = True
        st["last_start"] = int(started)
        log_event("info", "Bắt đầu luồng (ffmpeg)", cam_id)

        def _hint(line: str) -> str:
            low = line.lower()
            if "5xx" in low or "server returned 5" in low or "453" in low or "454" in low or "461" in low:
                return ("Camera từ chối kết nối RTSP (có thể đã đủ số phiên đồng thời — "
                        "kiểm tra Frigate/go2rtc/app hãng đang chiếm stream): " + line)
            if "401" in low or "unauthorized" in low or "authentication" in low:
                return "Sai tài khoản/mật khẩu RTSP hoặc thiếu xác thực: " + line
            if "connection refused" in low or "timed out" in low or "no route" in low:
                return "Không kết nối được tới camera (IP/port/mạng): " + line
            if "invalid data found" in low or "could not find codec" in low:
                return "Luồng RTSP không hợp lệ hoặc sai đường dẫn: " + line
            return line

        def _drain(p, cid, status):
            warned = False
            try:
                for line in p.stderr:
                    line = line.strip()
                    if not line:
                        continue
                    status["log"].append(line)
                    status["last_error"] = _hint(line)
                    low = line.lower()
                    if ("5xx" in low or "server returned 5" in low
                            or " 453 " in low or "453 " in low
                            or "500 internal" in low or "503 " in low or "461 " in low):
                        status["rate_limited"] = True
                    if not warned:
                        warned = True
                        log_event("error", status["last_error"], cid)
            except Exception:
                pass

        st["rate_limited"] = False
        threading.Thread(target=_drain, args=(proc, cam_id, st), daemon=True).start()
        proc.wait()
        st["running"] = False
        if ffmpeg_processes.get(cam_id) is proc:
            ffmpeg_processes.pop(cam_id, None)
        if token.is_set():
            break
        # Backoff:
        #  - vừa chạy ổn trong >10s: chờ ngắn rồi thử lại
        #  - camera trả 5XX (đủ phiên / chống spam → khoá tạm): chờ DÀI để nhả khoá
        #  - lỗi nhanh khác: chờ ngắn
        ran = time.time() - started
        if ran > 10:
            backoff = 5
            log_event("warn", f"Luồng dừng sau {int(ran)}s, thử lại sau {backoff}s", cam_id)
        elif st.get("rate_limited"):
            backoff = min(max(backoff * 2, 15), 120)
            log_event("warn", f"Camera từ chối kết nối (5XX), chờ {backoff}s để nhả khoá", cam_id)
        else:
            backoff = min(max(backoff, 2), 30)
            log_event("warn", f"Kết nối lỗi, thử lại sau {backoff}s", cam_id)
        if token.wait(backoff):
            break
    with worker_lock:
        if worker_tokens.get(cam_id) is token:
            worker_tokens.pop(cam_id, None)
    cam_status.setdefault(cam_id, {})["running"] = False

def stop_camera_worker(cam_id: str):
    with worker_lock:
        token = worker_tokens.pop(cam_id, None)
    if token:
        token.set()
    proc = ffmpeg_processes.get(cam_id)
    if proc and proc.poll() is None:
        proc.terminate()

def ensure_live(cam_id: str):
    """Gọi khi có request xem live: đánh dấu viewer và bật/khôi phục luồng nếu cần."""
    cam_info = CONFIG.get("cameras", {}).get(cam_id)
    if not cam_info:
        return
    mark_viewer(cam_id)
    st = cam_status.setdefault(cam_id, {})
    now = time.time()
    if cam_info.get("record_mode", "continuous") == "off":
        if not st.get("running"):
            start_camera_worker(cam_id)
        return
    # Camera có ghi hình: nếu nhánh live đang tắt (tiết kiệm CPU) thì khởi động lại
    if st.get("_live_mode") is False and now - st.get("_mode_ts", 0) > 5:
        st["_mode_ts"] = now
        proc = ffmpeg_processes.get(cam_id)
        if proc and proc.poll() is None:
            proc.terminate()

def live_supervisor():
    """Bật/tắt nhánh live theo số người xem để tiết kiệm CPU/RAM."""
    while True:
        try:
            now = time.time()
            for cam_id, cam in list(CONFIG.get("cameras", {}).items()):
                st = cam_status.get(cam_id, {})
                if cam.get("record_mode", "continuous") == "off":
                    last = cam_viewers.get(cam_id, 0)
                    if viewers_active(cam_id) and not st.get("running"):
                        log_event("info", "Có người xem, khởi động live", cam_id)
                        start_camera_worker(cam_id)
                    elif st.get("running") and last and (now - last) > LIVE_IDLE_STOP_SEC:
                        log_event("info", f"Không có người xem {LIVE_IDLE_STOP_SEC}s, dừng live", cam_id)
                        stop_camera_worker(cam_id)
                elif st.get("running") and st.get("_live_mode") is not None:
                    desired = viewers_active(cam_id) or not _needs_transcode(cam_id)
                    if desired != st.get("_live_mode") and now - st.get("_mode_ts", 0) > 5:
                        st["_mode_ts"] = now
                        log_event("info", "Đổi nhánh live theo người xem (tiết kiệm CPU)", cam_id)
                        proc = ffmpeg_processes.get(cam_id)
                        if proc and proc.poll() is None:
                            proc.terminate()
            disk = read_disk()
            thr = float(CONFIG.get("max_disk_percent", 85) or 0)
            if thr and disk["disk_percent"] > thr and now - _disk_warn["t"] > 600:
                _disk_warn["t"] = now
                log_event("warn", f"Ổ đĩa đã dùng {disk['disk_percent']}% (ngưỡng {thr}%): sẽ tự xoá video cũ nhất")
        except Exception as e:
            log_event("warn", f"live_supervisor: {e}")
        time.sleep(5)

# Luồng quản lý bộ đệm cho chế độ "Chỉ lưu khi có chuyển động (motion)"
def motion_buffer_manager():
    while True:
        now = time.time()
        for cam_id, cam_info in list(CONFIG.get("cameras", {}).items()):
            if cam_info.get("record_mode") != "motion":
                continue
            cam_buf_dir = os.path.join(buffer_root(), cam_id)
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
    room: str = ""                   # Nhóm/phòng để lọc camera
    tags: str = ""                   # Nhãn tự do, cách nhau dấu phẩy

class StorageSettings(BaseModel):
    retention_days: float
    max_storage_gb: float
    max_disk_percent: float
    post_motion_sec: int
    ram_buffer: bool = True          # True: đệm motion trong RAM; False: ghi ra đĩa

class OnvifProbeReq(BaseModel):
    host: str
    port: int = 80
    username: str = ""
    password: str = ""

class BulkCamerasReq(BaseModel):
    items: List[CameraItem]

class ClientLogReq(BaseModel):
    level: str = "warn"
    message: str
    cam_id: str = ""

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
        "ram_buffer": bool(CONFIG.get("ram_buffer", True)),
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
    CONFIG["ram_buffer"] = settings.ram_buffer
    save_config(CONFIG)
    deleted = run_cleanup_now()
    return {"status": "ok", "deleted_files": deleted}

@app.post("/api/storage/clean")
def manual_clean():
    deleted = run_cleanup_now()
    return {"status": "ok", "deleted_files": deleted}

@app.get("/api/stats")
def api_stats():
    return collect_stats()

@app.get("/api/stats/history")
def api_stats_history():
    return {"samples": list(stats_history)}

@app.get("/api/latest_snapshots")
def api_latest_snapshots():
    """Ảnh snapshot mới nhất của mỗi camera (làm ảnh đại diện cho lưới camera)."""
    out: Dict[str, str] = {}
    try:
        c = db_conn().cursor()
        c.execute("""
            SELECT e.cam_id, e.snapshot FROM events e
            JOIN (SELECT cam_id, MAX(timestamp) AS mt FROM events
                  WHERE snapshot IS NOT NULL GROUP BY cam_id) m
              ON e.cam_id = m.cam_id AND e.timestamp = m.mt
        """)
        for cam_id, snap in c.fetchall():
            if snap:
                out[cam_id] = f"/snapshots/{snap}"
    except Exception:
        pass
    return out

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
        "video_codec": item.video_codec,
        "room": item.room,
        "tags": item.tags,
    }
    save_config(CONFIG)
    start_camera_worker(cam_id)
    return {"status": "ok", "cameras": CONFIG["cameras"]}

@app.post("/api/cameras/bulk")
def bulk_add_cameras(req: BulkCamerasReq):
    """Thêm nhiều camera cùng lúc (cùng mẫu cấu hình)."""
    added = 0
    for item in req.items:
        cam_id = item.cam_id.strip()
        if not cam_id:
            continue
        stop_camera_worker(cam_id)
        CONFIG.setdefault("cameras", {})[cam_id] = {
            "name": item.name, "brand": item.brand,
            "rtsp_url": item.rtsp_url.strip(),
            "sub_rtsp_url": (item.sub_rtsp_url or "").strip(),
            "rtsp_transport": item.rtsp_transport,
            "audio_mode": item.audio_mode, "record_mode": item.record_mode,
            "video_codec": item.video_codec, "room": item.room, "tags": item.tags,
        }
        start_camera_worker(cam_id)
        added += 1
    save_config(CONFIG)
    log_event("info", f"Thêm hàng loạt {added} camera")
    return {"status": "ok", "added": added, "cameras": CONFIG["cameras"]}

@app.get("/api/onvif/discover")
def api_onvif_discover(timeout: float = 3.0):
    try:
        return {"devices": onvif_discover(timeout)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"WS-Discovery lỗi: {e}")

@app.post("/api/onvif/probe")
def api_onvif_probe(req: OnvifProbeReq):
    try:
        return onvif_probe(req.host, req.port, req.username, req.password)
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        try:
            body = (e.read() or b"").decode(errors="replace")
        except Exception:
            body = ""
        low = body.lower()
        if e.code in (401, 403) or any(k in low for k in (
                "not authorized", "invalid username", "invalidsecurity",
                "notauthorized", "unauthorized", "authentication")):
            raise HTTPException(status_code=401, detail="Sai tài khoản/mật khẩu ONVIF (hoặc camera chưa bật ONVIF)")
        texts = re.findall(r"<[^>]*Text[^>]*>([^<]*)<", body)
        reason = next((t.strip() for t in texts if t.strip()), "")
        raise HTTPException(status_code=502, detail=f"Camera ONVIF lỗi HTTP {e.code}" + (f": {reason}" if reason else ""))
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Không kết nối được camera ONVIF: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ONVIF probe lỗi: {e}")

@app.post("/api/live/{cam_id}")
def api_live_heartbeat(cam_id: str):
    if cam_id not in CONFIG.get("cameras", {}):
        raise HTTPException(status_code=404, detail="Camera không tồn tại")
    ensure_live(cam_id)
    return {"status": "ok", "running": bool(cam_status.get(cam_id, {}).get("running"))}

@app.post("/api/client_log")
def api_client_log(req: ClientLogReq):
    """Nhận lỗi từ trình duyệt (HLS/playback) để hiện trong Nhật ký."""
    level = req.level if req.level in ("info", "warn", "error") else "warn"
    log_event(level, "UI: " + req.message[:500], req.cam_id)
    return {"status": "ok"}

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
    log_event("info", f"Phát hiện chuyển động: {label}", cam_id)

    # Ưu tiên chụp Snapshot từ luồng phụ để lấy ảnh cực nhanh
    snap_rtsp = cam_info.get("sub_rtsp_url") or cam_info["rtsp_url"]
    try:
        subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *_input_args(snap_rtsp, cam_info),
            "-i", snap_rtsp,
            "-frames:v", "1", "-q:v", "3", snap_path
        ])
    except Exception as e:
        print(f"[LiteNVR] Không chụp được snapshot cho {cam_id}: {e}", flush=True)
        log_event("warn", f"Không chụp được snapshot: {e}", cam_id)

    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (cam_id, ts, date_str, time_str, label, snap_filename)
    )
    conn.commit()
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
                log_event("info", "Đã kết nối Home Assistant, đang lắng nghe chuyển động")
            except Exception as e:
                hass_state["connected"] = False
                hass_state["last_error"] = str(e)
                print(f"[LiteNVR] Lỗi kết nối HA: {e}", flush=True)
                log_event("error", f"Lỗi kết nối Home Assistant: {e}")
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
            log_event("warn", f"Mất kết nối Home Assistant, thử lại: {e}")
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

    conn = db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, timestamp, time_str, label, snapshot FROM events WHERE cam_id=? AND date_str=? ORDER BY timestamp DESC",
        (cam_id, date)
    )
    events = []
    for row in c.fetchall():
        try:
            h, m, s = map(int, row[2].replace("-", ":").split(":"))
        except Exception:
            continue
        events.append({
            "id": row[0],
            "timestamp": row[1],
            "time": row[2],
            "sec_of_day": h * 3600 + m * 60 + s,
            "label": row[3],
            "snapshot": f"/snapshots/{row[4]}"
        })
    return {
        "segments": segments,
        "events": events,
        "post_motion_sec": CONFIG.get("post_motion_sec", 60),
    }

@app.get("/api/logs")
def get_logs(limit: int = 200):
    """Nhật ký hệ thống + trạng thái lỗi từng camera để UI hiển thị và gợi ý cách sửa."""
    cams = {}
    for cam_id, cam in CONFIG.get("cameras", {}).items():
        st = cam_status.get(cam_id, {})
        cams[cam_id] = {
            "name": cam.get("name", cam_id),
            "running": bool(st.get("running")),
            "last_error": st.get("last_error", ""),
            "rate_limited": bool(st.get("rate_limited", False)),
            "detect_failed": bool(st.get("detect_failed", False)),
            "detected_codec": st.get("detected_codec", ""),
        }
    events = list(APP_LOG)[-max(1, min(limit, 300)):]
    events.reverse()
    return {"events": events, "cameras": cams}

@app.get("/api/status/{cam_id}")
def get_cam_status(cam_id: str):
    hls_path = os.path.join(HLS_DIR, cam_id, "live.m3u8")
    hls_ready = os.path.exists(hls_path) and (time.time() - os.path.getmtime(hls_path) < 15)
    st = cam_status.get(cam_id, {})
    return {
        "running": bool(st.get("running")),
        "hls_ready": hls_ready,
        "detected_codec": st.get("detected_codec", ""),
        "detected_profile": st.get("detected_profile", ""),
        "detected_pix_fmt": st.get("detected_pix_fmt", ""),
        "detect_failed": bool(st.get("detect_failed", False)),
        "rate_limited": bool(st.get("rate_limited", False)),
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
