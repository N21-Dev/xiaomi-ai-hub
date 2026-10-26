"""安卓安装包分发目录的解析回归。

只测纯函数：路由怎么接、响应长什么样由隔离容器里的 curl 验证覆盖。
"""

import json
import tempfile
import unittest
from pathlib import Path

from xiaomi_ai_bridge import app_release


class AndroidReleaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.dir = self.state / "android"
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, name="Hub-1.3.0.apk", payload=b"PK\x03\x04fake-apk", **overrides):
        (self.dir / name).write_bytes(payload)
        manifest = {
            "versionCode": 6,
            "versionName": "1.3.0",
            "file": name,
            "sha256": "a" * 64,
        }
        manifest.update(overrides)
        (self.dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_missing_directory(self):
        self.assertIsNone(app_release.resolve(self.state / "nope"))

    def test_missing_manifest(self):
        (self.dir / "Hub-1.3.0.apk").write_bytes(b"x")
        self.assertIsNone(app_release.resolve(self.state))

    def test_broken_manifest(self):
        (self.dir / "manifest.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(app_release.resolve(self.state))

    def test_valid_release(self):
        self.publish(notes="修复写操作", publishedAt="2026-10-20T19:00:00+08:00")
        resolved = app_release.resolve(self.state)
        self.assertIsNotNone(resolved)
        manifest, path = resolved
        self.assertEqual(manifest["versionCode"], 6)
        self.assertEqual(manifest["versionName"], "1.3.0")
        self.assertEqual(manifest["size"], len(b"PK\x03\x04fake-apk"))
        self.assertEqual(manifest["sha256"], "a" * 64)
        self.assertEqual(manifest["notes"], "修复写操作")
        self.assertEqual(manifest["publishedAt"], "2026-10-20T19:00:00+08:00")
        self.assertEqual(path.name, "Hub-1.3.0.apk")

    def test_rejects_path_traversal(self):
        (self.state / "outside.apk").write_bytes(b"x")
        self.publish(file="../outside.apk")
        self.assertIsNone(app_release.resolve(self.state), "不能顺着 .. 跑到目录外")

    def test_rejects_non_apk(self):
        (self.dir / "notes.txt").write_bytes(b"x")
        self.publish(file="notes.txt")
        self.assertIsNone(app_release.resolve(self.state))

    def test_rejects_missing_file(self):
        self.publish()
        (self.dir / "Hub-1.3.0.apk").unlink()
        self.assertIsNone(app_release.resolve(self.state))

    def test_rejects_empty_file(self):
        self.publish(payload=b"")
        self.assertIsNone(app_release.resolve(self.state))

    def test_rejects_oversized_file(self):
        original = app_release.MAX_APK_BYTES
        app_release.MAX_APK_BYTES = 8
        try:
            self.publish(payload=b"0123456789")
            self.assertIsNone(app_release.resolve(self.state))
        finally:
            app_release.MAX_APK_BYTES = original

    def test_rejects_bad_version(self):
        self.publish(versionCode=0)
        self.assertIsNone(app_release.resolve(self.state))
        self.publish(versionCode="abc")
        self.assertIsNone(app_release.resolve(self.state))

    def test_rejects_empty_version_name(self):
        self.publish(versionName="  ")
        self.assertIsNone(app_release.resolve(self.state))


if __name__ == "__main__":
    unittest.main()
