import base64
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
SRC = os.path.join(APP_DIR, "main.py")


def load_app(env=None):
    os.environ["GO2RTC"] = "off"
    os.environ["NVR_AUTH"] = "on"
    os.environ["NVR_USER"] = "user1"
    os.environ["NVR_PASS"] = "pass1"
    if env:
        os.environ.update(env)
    sandbox = tempfile.mkdtemp(prefix="litenvr_test_")
    shm = os.path.join(sandbox, "shm")
    os.makedirs(shm, exist_ok=True)
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    src = src.replace('STORAGE_DIR = "/storage"', f'STORAGE_DIR = {sandbox!r}')
    src = src.replace('HLS_DIR = "/dev/shm/hls"', f'HLS_DIR = {os.path.join(shm, "hls")!r}')
    src = src.replace('BUFFER_DIR = "/dev/shm/buffer"', f'BUFFER_DIR = {os.path.join(shm, "buffer")!r}')
    mod_path = os.path.join(sandbox, "main.py")
    with open(mod_path, "w", encoding="utf-8") as f:
        f.write(src)
    cwd = os.getcwd()
    os.chdir(APP_DIR)
    try:
        spec = importlib.util.spec_from_file_location("litenvr_test_main", mod_path)
        m = importlib.util.module_from_spec(spec)
        sys.modules["litenvr_test_main"] = m
        spec.loader.exec_module(m)
    finally:
        os.chdir(cwd)
    return sandbox, m


def auth(u="user1", p="pass1"):
    tok = base64.b64encode(f"{u}:{p}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


class _AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.m.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)


class ApiTests(_AppCase):
    def test_health_and_auth(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/system").status_code, 401)
        self.assertEqual(self.client.get("/api/system", headers=auth("user1", "wrong")).status_code, 401)
        self.assertEqual(self.client.get("/api/system", headers=auth()).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_camera_lifecycle(self):
        payload = {"cam_id": "cam1", "name": "Cam", "brand": "custom",
                   "rtsp_url": "rtsp://127.0.0.1:1/x", "record_mode": "continuous"}
        self.assertEqual(self.client.post("/api/cameras", json=payload, headers=auth()).status_code, 200)
        self.assertIn("cam1", self.client.get("/api/cameras", headers=auth()).json())
        self.assertEqual(self.client.delete("/api/cameras/cam1", headers=auth()).status_code, 200)
        self.assertEqual(self.client.get("/api/cameras", headers=auth()).json(), {})

    def test_storage_motion_timeline(self):
        payload = {"cam_id": "cam1", "name": "Cam", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "record_mode": "motion", "video_codec": "h264"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        r = self.client.post("/api/storage", json={"retention_days": 3, "max_storage_gb": 1,
                                                   "max_disk_percent": 90, "post_motion_sec": 30},
                             headers=auth())
        self.assertEqual(r.status_code, 200)
        self.client.post("/api/motion", json={"cam_id": "cam1", "label": "test"}, headers=auth())
        d = time.strftime("%Y-%m-%d")
        r = self.client.get(f"/api/timeline/cam1?date={d}", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("segments", r.json())

    def test_play_missing_and_status(self):
        payload = {"cam_id": "cam1", "name": "Cam", "rtsp_url": "rtsp://127.0.0.1:1/x",
                   "record_mode": "off"}
        self.client.post("/api/cameras", json=payload, headers=auth())
        r = self.client.get("/api/status/cam1", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("running", r.json())
        self.assertEqual(self.client.get("/api/play/cam1/nope.mp4", headers=auth()).status_code, 404)

    def test_stats(self):
        r = self.client.get("/api/stats", headers=auth())
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn("mem_total_mb", j)
        self.assertTrue(0 <= j["mem_percent"] <= 100)
        self.assertTrue(0 <= j["disk_percent"] <= 100)

    def test_viewer_playback_permission(self):
        self.client.post("/api/cameras", json={"cam_id": "cam1", "name": "Cam",
                                               "rtsp_url": "rtsp://127.0.0.1:1/x"}, headers=auth())
        r = self.client.post("/api/users", json={
            "username": "viewer1", "password": "secret1", "role": "user",
            "perms": {"live": True, "playback": False, "events": False, "export": False, "ptz": False, "lock": False},
        }, headers=auth())
        self.assertEqual(r.status_code, 200)
        v = auth("viewer1", "secret1")
        self.assertEqual(self.client.get("/api/cameras", headers=v).status_code, 200)
        self.assertEqual(self.client.get("/api/drive/play/cam1/x.mp4", headers=v).status_code, 403)

    def test_config_export_import(self):
        self.assertEqual(self.client.get("/api/config/export", headers=auth()).status_code, 200)

    def test_motion_detect_settings(self):
        r = self.client.get("/api/motion_detect", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])
        r = self.client.post("/api/motion_detect", json={"enabled": True, "sensitivity": 0.08,
                                                         "cooldown_sec": 7, "source": "main"},
                             headers=auth())
        self.assertEqual(r.status_code, 200)
        j = self.client.get("/api/motion_detect", headers=auth()).json()
        self.assertTrue(j["enabled"])
        self.assertAlmostEqual(j["sensitivity"], 0.08, places=3)
        self.assertEqual(j["source"], "main")
        self.client.post("/api/motion_detect", json={"enabled": False}, headers=auth())
        self.assertFalse(self.client.get("/api/motion_detect", headers=auth()).json()["enabled"])

    def test_events_search(self):
        conn = self.m.db_conn()
        conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                     "VALUES (?,?,?,?,?,?)", ("camS", 100, "2026-01-02", "10-00-00", "Người lạ", "s.jpg"))
        conn.execute("INSERT INTO events (cam_id, timestamp, date_str, time_str, label, snapshot) "
                     "VALUES (?,?,?,?,?,?)", ("camT", 200, "2026-02-03", "11-00-00", "Người quen", "t.jpg"))
        conn.commit()
        j = self.client.get("/api/events?cam_id=camS", headers=auth()).json()
        self.assertEqual(j["count"], 1)
        self.assertEqual(j["events"][0]["label"], "Người lạ")
        self.assertEqual(j["events"][0]["snapshot"], "/snapshots/s.jpg")
        self.assertEqual(self.client.get("/api/events?q=Người", headers=auth()).json()["count"], 2)
        self.assertEqual(self.client.get("/api/events?q=khong-co", headers=auth()).json()["count"], 0)
        j = self.client.get("/api/events?date_from=2026-02-01&date_to=2026-02-28", headers=auth()).json()
        self.assertEqual(j["count"], 1)
        self.assertEqual(j["events"][0]["cam_id"], "camT")

    def test_thumbnail_validation(self):
        self.assertEqual(self.client.get("/api/thumb/nope/x.mp4", headers=auth()).status_code, 404)
        self.client.post("/api/cameras", json={"cam_id": "camThumb", "name": "T", "rtsp_url": "rtsp://x"},
                         headers=auth())
        self.assertEqual(self.client.get("/api/thumb/camThumb/bad.txt", headers=auth()).status_code, 400)
        self.assertEqual(self.client.get("/api/thumb/camThumb/missing.mp4", headers=auth()).status_code, 404)
        self.client.delete("/api/cameras/camThumb", headers=auth())

    def test_thumbnail_generation(self):
        if subprocess.call(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            self.skipTest("ffmpeg không có sẵn")
        self.client.post("/api/cameras", json={"cam_id": "camThumb2", "name": "T2", "rtsp_url": "rtsp://x"},
                         headers=auth())
        rec = os.path.join(self.m.RECORDINGS_DIR, "camThumb2")
        os.makedirs(rec, exist_ok=True)
        subprocess.check_call(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5",
                               "-t", "2", "-pix_fmt", "yuv420p", os.path.join(rec, "2026-01-01_00-00-00.mp4")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = self.client.get("/api/thumb/camThumb2/2026-01-01_00-00-00.mp4", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertGreater(len(r.content), 0)
        self.client.delete("/api/cameras/camThumb2", headers=auth())


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()

    def _cam(self, **kw):
        cam = {"name": "C", "rtsp_url": "rtsp://cam.invalid/main", "sub_rtsp_url": "rtsp://cam.invalid/sub",
               "record_mode": "continuous", "video_codec": "auto"}
        cam.update(kw)
        return cam

    def _values(self, cmd, flag):
        return [cmd[i + 1] for i, a in enumerate(cmd) if a == flag]

    def test_filter_and_decoder_threads(self):
        base = self.m._ffmpeg_base_args()
        self.assertEqual(self._values(base, "-filter_threads"), ["2"])
        self.assertEqual(self._values(base, "-filter_complex_threads"), ["2"])
        opts = self.m._input_args("rtsp://cam.invalid/main", {"rtsp_transport": "udp"})
        self.assertEqual(self._values(opts, "-threads"), ["2"])
        self.assertEqual(self._values(opts, "-rtsp_transport"), ["udp"])
        self.assertNotIn("-rtsp_transport", self.m._input_args("http://cam.invalid/x", {}))

    def test_only_required_sources(self):
        base = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="continuous", video_codec="copy"), include_live=False)
        self.assertEqual(base.count("-i"), 1)
        off = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="off", video_codec="copy"), include_live=True)
        self.assertEqual(off.count("-i"), 1)
        self.assertIn("sub", off[off.index("-i") + 1])

    def test_runtime_live_downscale(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="copy", video_codec="h264"))
        self.assertIn("-vf", cmd)
        vf = self._values(cmd, "-vf")[0]
        self.assertIn("min(", vf)
        self.assertIn("720", vf)
        self.assertIn("ih", vf)

    def test_motion_detect_command(self):
        cmd = self.m.build_motion_detect_cmd("cam", self._cam())
        joined = " ".join(cmd)
        self.assertIn("select='gt(scene", joined)
        self.assertIn("showinfo", joined)
        self.assertEqual(cmd[cmd.index("-i") + 1], self._cam()["sub_rtsp_url"])

    def test_explicit_h264_recording_uses_h264_while_live_copy(self):
        cmd = self.m.build_ffmpeg_cmd("cam", self._cam(record_mode="continuous", video_codec="copy"), video_codec="h264")
        self.assertIn("libx264", cmd)
        self.assertIn("copy", cmd)


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox, cls.m = load_app()

    def test_terminate_process_reaps(self):
        proc = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(60)"])
        self.m._terminate_process(proc, timeout=2)
        self.assertIsNotNone(proc.poll())

    def test_snapshot_slots_bounded(self):
        m = self.m
        m.cam_source_urls = lambda cam_id, cam_info: (cam_info["rtsp_url"], cam_info["rtsp_url"])
        lock = threading.Lock()
        state = {"active": 0, "max": 0}

        class FakeProc:
            def __init__(self, *a, **k):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                self.returncode = None
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                time.sleep(0.05)
                with lock:
                    if self.returncode is None:
                        state["active"] -= 1
                self.returncode = 0
                return 0
            def terminate(self):
                self.returncode = 0
            def kill(self):
                self.returncode = 0

        real = subprocess.Popen
        subprocess.Popen = FakeProc
        try:
            paths = [m._start_snapshot(f"cam{i}", {"rtsp_url": "rtsp://x"}, os.path.join(self.sandbox, f"s{i}.jpg"))
                     for i in range(5)]
            self.assertGreaterEqual(paths.count(""), 2)
            deadline = time.time() + 3
            while time.time() < deadline and (m._snapshot_active or m._snapshot_processes):
                time.sleep(0.05)
            self.assertEqual(m._snapshot_active, {})
            self.assertLessEqual(state["max"], 2)
            for _ in range(2):
                m._snapshot_slots.acquire(blocking=False)
            for _ in range(2):
                m._snapshot_slots.release()
        finally:
            subprocess.Popen = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
