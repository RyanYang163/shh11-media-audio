"""端到端功能测试：把应用真的跑起来，逐个打接口。

这是「功能可用」这一层的验证 —— 官方审核会把应用装到真机上点一遍，
这里用同样的顺序在开发机上先走一遍：启动 → 设白名单 → 扫描 → 探测 → 提取 → 查结果。

应用以 TCP 模式启动（Windows 上没有 Unix socket），跑的是与 deb 包内**同一份**代码，
只是换了监听方式。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tnasapp.paths import AppPaths  # noqa: E402

from app import container, main as app_main  # noqa: E402
from tests import synth  # noqa: E402

APP_ID = app_main.APP_ID


class ApiClient:
    def __init__(self, base):
        self.base = base

    def request(self, method, path, payload=None, raw=False):
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
                if raw:
                    return response.status, body
                return response.status, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body}

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)


class AppEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh11-api-")
        cls.media_dir = os.path.join(cls.tmp, "media")
        cls.output_dir = os.path.join(cls.tmp, "out")
        cls.outside_dir = os.path.join(cls.tmp, "outside")
        os.makedirs(cls.media_dir)
        os.makedirs(cls.outside_dir)

        # 造两个双轨媒体文件
        cls.audio = [b"AUDIO%02d" % i + bytes([0x41 + i]) * (200 + i * 11) for i in range(3)]
        cls.video = [b"VIDEO%02d" % i + bytes([0x61 + i]) * (900 + i * 13) for i in range(3)]
        data, _, _ = synth.build_mp4(cls.audio, cls.video)
        with open(os.path.join(cls.media_dir, "clip-one.mp4"), "wb") as fh:
            fh.write(data)
        with open(os.path.join(cls.media_dir, "clip-two.mp4"), "wb") as fh:
            fh.write(synth.build_mp4(cls.audio[:2], cls.video[:2])[0])
        with open(os.path.join(cls.media_dir, "notes.txt"), "wb") as fh:
            fh.write(b"not a media file")

        # install_dir 指向仓库根：webui_dir 由它推导，测试要能取到真的前端文件
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = AppPaths(APP_ID, install_dir=repo,
                         data_dir=os.path.join(cls.tmp, "data"))
        cls.app = app_main.create_app(paths=paths, log_level="ERROR")
        # 白名单在启动前就设好 —— 用例之间不能有执行顺序依赖
        # （unittest 按方法名字母序跑，第一版就栽在「探测早于设白名单」上）
        cls.app.set_allowed_roots([cls.media_dir, cls.output_dir])
        cls.server = cls.app.run(host="127.0.0.1", port=0, background=True)
        cls.client = ApiClient("http://127.0.0.1:%d" % cls.server.server_address[1])
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.shutdown()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 辅助

    @classmethod
    def _ensure_scan(cls):
        """跑一次扫描并缓存结果，让依赖它的用例彼此独立。"""
        if getattr(cls, "_scan_job", None) is not None:
            return cls._scan_job
        status, body = cls.client.post(
            "/api/jobs", {"type": "scan", "params": {"roots": [cls.media_dir]}})
        assert status == 201, body
        cls._scan_job = cls._wait_job_class(body["job"]["id"])
        return cls._scan_job

    @classmethod
    def _wait_job_class(cls, job_id, timeout=60):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            _status, body = cls.client.get("/api/jobs/%d" % job_id)
            job = body["job"]
            if job["state"] in ("completed", "failed", "canceled"):
                return job
            time.sleep(0.1)
        return job

    @classmethod
    def _ensure_extract(cls):
        if getattr(cls, "_extract_job", None) is not None:
            return cls._extract_job
        status, body = cls.client.post("/api/jobs", {
            "type": "extract",
            "params": {"inputs": [os.path.join(cls.media_dir, "clip-one.mp4")],
                       "output_dir": cls.output_dir, "track_id": "auto"},
        })
        assert status == 201, body
        cls._extract_job = cls._wait_job_class(body["job"]["id"])
        return cls._extract_job

    # ---------------------------------------------------------- 基础

    def test_health(self):
        status, body = self.client.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["app"], APP_ID)

    def test_app_info(self):
        status, body = self.client.get("/api/app")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], app_main.APP_VERSION)
        self.assertIn("scan", body["job_types"])
        self.assertIn("extract", body["job_types"])

    def test_index_html_served(self):
        status, body = self.client.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"media.js", body)
        # 相对路径：不能出现 src="/..." 否则在 /<appid>/ 前缀下白屏（审核项 F7）
        self.assertNotIn(b'src="/', body)

    def test_prefix_compatibility(self):
        for prefix in ("", "/" + APP_ID, "/v2/proxy/" + APP_ID):
            status, body = self.client.get(prefix + "/api/app")
            self.assertEqual(status, 200, prefix)
            self.assertEqual(body["app"], APP_ID)

    def test_engines_endpoint(self):
        status, body = self.client.get("/api/engines")
        self.assertEqual(status, 200)
        self.assertIn("engine_detail", body)
        self.assertTrue(body["presets"], "应列出转码预设")
        names = {preset["id"] for preset in body["presets"]}
        self.assertIn("mp3_320", names)
        self.assertIn("flac", names)

    # ---------------------------------------------------------- 白名单

    def test_empty_whitelist_refuses_everything(self):
        saved = list(self.app.allowed.roots())
        try:
            self.app.set_allowed_roots([])
            status, body = self.client.get("/api/media/detect?path=" + self.media_dir)
            self.assertEqual(status, 403)
            self.assertFalse(body["ok"])
            status, body = self.client.get("/api/fs/list?path=" + self.media_dir)
            self.assertFalse(body.get("ok", False))
        finally:
            self.app.set_allowed_roots(saved)

    def test_set_allowed_root(self):
        status, body = self.client.post("/api/settings",
                                        {"allowed_roots": [self.media_dir, self.output_dir]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.client.get("/api/settings")
        self.assertEqual(sorted(body["settings"]["allowed_roots"]),
                         sorted([self.media_dir, self.output_dir]))

    def test_outside_path_still_refused(self):
        status, body = self.client.get("/api/media/detect?path=" + self.outside_dir)
        self.assertEqual(status, 403)

    def test_path_traversal_refused(self):
        status, body = self.client.get(
            "/api/media/detect?path=" + os.path.join(self.media_dir, "..", "outside"))
        self.assertEqual(status, 403)

    def test_fs_list_inside_whitelist(self):
        status, body = self.client.get("/api/fs/list?path=" + self.media_dir)
        self.assertEqual(status, 200)
        names = {entry["name"] for entry in body["entries"]}
        self.assertIn("clip-one.mp4", names)
        self.assertIn("notes.txt", names)

    # ---------------------------------------------------------- 探测

    def test_detect_file(self):
        status, body = self.client.get(
            "/api/media/detect?path=" + os.path.join(self.media_dir, "clip-one.mp4"))
        self.assertEqual(status, 200)
        self.assertFalse(body["is_dir"])
        self.assertTrue(body["supported"])

    def test_probe_reports_two_tracks(self):
        status, body = self.client.get(
            "/api/media/probe?path=" + os.path.join(self.media_dir, "clip-one.mp4"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["track_count"], 2)
        self.assertEqual(len(body["audio_tracks"]), 1)
        audio = body["audio_tracks"][0]
        self.assertEqual(audio["codec"], "mp4a")
        self.assertEqual(audio["channels"], 2)
        self.assertEqual(audio["sample_rate"], 44100)

    def test_probe_rejects_non_media(self):
        status, body = self.client.get(
            "/api/media/probe?path=" + os.path.join(self.media_dir, "notes.txt"))
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertTrue(body["error"])
        # 不受支持的文件不入库 —— 随手探测一个 txt 不该污染媒体库
        _status, listing = self.client.get("/api/media/files?limit=200&q=notes.txt")
        self.assertEqual(listing["total"], 0)

    # ---------------------------------------------------------- 任务

    def _wait_job(self, job_id, timeout=60):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            status, body = self.client.get("/api/jobs/%d" % job_id)
            job = body["job"]
            if job["state"] in ("completed", "failed", "canceled"):
                return job
            time.sleep(0.1)
        return job

    def test_scan_job_populates_library(self):
        job = self._ensure_scan()
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        self.assertEqual(job["result"]["found"], 2, "只应收录两个媒体文件")
        self.assertEqual(job["result"]["failed"], 0)

        status, body = self.client.get("/api/media/files?limit=50")
        self.assertEqual(body["total"], 2)
        paths = {file["path"] for file in body["files"]}
        self.assertIn(os.path.join(self.media_dir, "clip-one.mp4"), paths)

    def test_scan_job_logs_present(self):
        self._ensure_scan()
        status, body = self.client.get("/api/jobs?limit=20")
        scan_jobs = [job for job in body["jobs"] if job["type"] == "scan"]
        self.assertTrue(scan_jobs)
        status, body = self.client.get("/api/jobs/%d/logs" % scan_jobs[0]["id"])
        self.assertTrue(body["logs"])
        self.assertIn("任务开始", body["logs"][0]["message"])

    def test_extract_job_produces_byte_identical_audio(self):
        job = self._ensure_extract()
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        self.assertEqual(job["result"]["succeeded"], 1)
        self.assertEqual(job["result"]["failed"], 0)

        status, body = self.client.get("/api/media/results?job_id=%d" % job["id"])
        self.assertEqual(len(body["results"]), 1)
        record = body["results"][0]
        self.assertEqual(record["mode"], "stream-copy")
        self.assertTrue(os.path.exists(record["output"]))

        # 关键断言：抽出来的音频负载必须与源文件里的音轨逐字节相同
        self.assertLess(os.path.getsize(record["output"]),
                        os.path.getsize(os.path.join(self.media_dir, "clip-one.mp4")))
        info = container.parse_mp4(record["output"])
        self.assertEqual(len(info["tracks"]), 1)
        extracted = bytearray()
        with open(record["output"], "rb") as fh:
            for offset, size in zip(info["tracks"][0]["index"]["chunk_offsets"],
                                    info["tracks"][0]["chunk_sizes"]):
                fh.seek(offset)
                extracted.extend(fh.read(size))
        self.assertEqual(bytes(extracted), b"".join(self.audio))

    def test_extract_from_directory_walks_recursively(self):
        self._ensure_scan()
        status, body = self.client.post("/api/jobs", {
            "type": "extract",
            "params": {"roots": [self.media_dir], "output_dir": self.output_dir},
        })
        job = self._wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["result"]["total"], 2, "目录里两个媒体文件都该被处理")

    def test_extract_refuses_output_outside_whitelist(self):
        status, body = self.client.post("/api/jobs", {
            "type": "extract",
            "params": {"inputs": [os.path.join(self.media_dir, "clip-one.mp4")],
                       "output_dir": self.outside_dir},
        })
        self.assertEqual(status, 201)
        job = self._wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("允许访问", job["error"])

    def test_extract_refuses_transcode_without_ffmpeg(self):
        status, body = self.client.post("/api/jobs", {
            "type": "extract",
            "params": {"inputs": [os.path.join(self.media_dir, "clip-one.mp4")],
                       "output_dir": self.output_dir, "preset": "mp3_320"},
        })
        job = self._wait_job(body["job"]["id"])
        # 本机没有 ffmpeg 时应当明确失败并给出可读原因；有 ffmpeg 时应当成功
        from app import ffmpeg as ffmpeg_engine

        if ffmpeg_engine.detect()["available"]:
            self.assertEqual(job["state"], "completed")
        else:
            self.assertEqual(job["state"], "failed")
            self.assertIn("ffmpeg", job["error"])

    def test_unknown_job_type_is_rejected(self):
        status, body = self.client.post("/api/jobs", {"type": "no-such-job"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_job_list_and_counts(self):
        self._ensure_scan()
        status, body = self.client.get("/api/jobs?limit=50")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["counts"]["total"], 3)
        self.assertGreaterEqual(body["counts"]["completed"], 2)

    # ---------------------------------------------------------- 汇总

    def test_summary_reflects_scan(self):
        self._ensure_scan()
        self._ensure_extract()
        status, body = self.client.get("/api/media/summary")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["with_audio"], 2)
        self.assertGreater(body["total_bytes"], 0)
        self.assertGreaterEqual(body["extracted"], 1)

    def test_logs_redact_credentials(self):
        """日志脱敏必须生效 —— 不能把 API Key / 密码写进日志。"""
        from tnasapp import logx

        sample = "api_key=abcd1234 password=hunter2 Authorization: Bearer tok3nvalue1234"
        masked = logx.redact(sample)
        for secret in ("abcd1234", "hunter2", "tok3nvalue1234"):
            self.assertNotIn(secret, masked)

    def test_engine_status_shape(self):
        status, body = self.client.get("/api/engines")
        detail = body["engine_detail"]
        for key in ("name", "available", "detail", "enables"):
            self.assertIn(key, detail)
        self.assertEqual(detail["name"], "ffmpeg")


if __name__ == "__main__":
    unittest.main()
