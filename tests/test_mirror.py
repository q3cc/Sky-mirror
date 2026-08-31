from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mirror.py"
SPEC = importlib.util.spec_from_file_location("sky_mirror", MODULE_PATH)
mirror = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mirror)

DOMESTIC_MIRROR_URL = "https://1829561701.share.123pan.cn/123pan/uN8cjv-0hjPH"


def config(part_limit: int = 1_000_000_000) -> dict:
    return {
        "schemaVersion": 1,
        "app": {
            "title": "Sky: Children of the Light",
            "packageName": "com.tgc.sky.android",
            "playUrl": "https://play.google.com/store/apps/details?id=com.tgc.sky.android",
        },
        "download": {
            "architecture": "arm64",
            "locales": [],
            "includeSplits": True,
            "includeExtras": True,
            "includeDexMetadata": False,
        },
        "release": {
            "tagPrefix": "sky-android",
            "bundleName": "sky-android-arm64.zip",
            "bundlePartMaxBytes": part_limit,
            "assetMaxBytes": 2_147_483_647,
            "domesticMirrorUrl": DOMESTIC_MIRROR_URL,
        },
    }


def details(version_name: str = "0.30.0", version_code: int = 300000) -> dict:
    return {
        "title": "Sky: Children of the Light",
        "packageName": "com.tgc.sky.android",
        "versionName": version_name,
        "versionCode": version_code,
        "developer": "thatgamecompany inc",
        "playUrl": "https://play.google.com/store/apps/details?id=com.tgc.sky.android",
    }


class MirrorTests(unittest.TestCase):
    def test_release_identity_ignores_probe_timestamp(self) -> None:
        first = mirror.build_probe_manifest(config(), details())
        second = mirror.build_probe_manifest(config(), details())
        first["probe"]["observedAt"] = "2026-01-01T00:00:00Z"
        second["probe"]["observedAt"] = "2026-08-30T00:00:00Z"
        self.assertEqual(mirror.release_identity(first), mirror.release_identity(second))

    def test_version_code_changes_identity(self) -> None:
        first = mirror.build_probe_manifest(config(), details(version_code=1))
        second = mirror.build_probe_manifest(config(), details(version_code=2))
        self.assertNotEqual(mirror.release_identity(first), mirror.release_identity(second))

    def test_tag_is_safe(self) -> None:
        tag = mirror.make_release_tag(config(), "0.30.0 beta / 1", 42)
        self.assertEqual(tag, "sky-android-0.30.0-beta-1-vc42")

    def test_finalize_creates_manifest_checksums_index_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            config_path = root / "config.json"
            probe_path = root / "probe.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            probe = mirror.build_probe_manifest(config(), details(version_code=77))
            probe_path.write_text(json.dumps(probe), encoding="utf-8")
            (input_dir / "com.tgc.sky.android-77.apk").write_bytes(b"base")
            (input_dir / "com.tgc.sky.android-77-config.arm64_v8a.apk").write_bytes(b"split")

            result = mirror.command_finalize(
                SimpleNamespace(
                    config=config_path,
                    probe=probe_path,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    repository="owner/Sky-mirror",
                )
            )

            self.assertEqual(result, 0)
            manifest = json.loads((output_dir / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["artifacts"]), 2)
            self.assertTrue(manifest["bundle"]["created"])
            self.assertTrue(manifest["bundle"]["singleFile"])
            zip_path = output_dir / "sky-android-arm64.zip"
            self.assertTrue(zip_path.is_file())
            with mirror.zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "INSTALL.txt",
                        "packages/com.tgc.sky.android-77.apk",
                        "packages/com.tgc.sky.android-77-config.arm64_v8a.apk",
                    },
                )
            self.assertTrue((output_dir / "download-index.json").is_file())
            self.assertIn("sky-android-arm64.zip", (output_dir / "SHA256SUMS.txt").read_text())
            release_notes = (output_dir / "release-notes.md").read_text(encoding="utf-8")
            self.assertIn("# Sky Android 镜像更新", release_notes)
            self.assertIn(DOMESTIC_MIRROR_URL, release_notes)
            self.assertEqual(manifest["release"]["domesticMirrorUrl"], DOMESTIC_MIRROR_URL)
            self.assertFalse((output_dir / "com.tgc.sky.android-77.apk").exists())

    def test_finalize_splits_zip_above_configured_limit_and_reassembles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            config_path = root / "config.json"
            probe_path = root / "probe.json"
            config_path.write_text(json.dumps(config(part_limit=100)), encoding="utf-8")
            probe = mirror.build_probe_manifest(config(part_limit=100), details(version_code=88))
            probe_path.write_text(json.dumps(probe), encoding="utf-8")
            (input_dir / "com.tgc.sky.android-88.apk").write_bytes(b"larger-than-one-byte")

            mirror.command_finalize(
                SimpleNamespace(
                    config=config_path,
                    probe=probe_path,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    repository="owner/Sky-mirror",
                )
            )
            manifest = json.loads((output_dir / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["bundle"]["created"])
            self.assertFalse(manifest["bundle"]["singleFile"])
            self.assertFalse((output_dir / "sky-android-arm64.zip").exists())
            self.assertGreater(len(manifest["bundle"]["parts"]), 1)
            self.assertTrue(all(part["size"] <= 100 for part in manifest["bundle"]["parts"]))
            rebuilt = b"".join(
                (output_dir / part["name"]).read_bytes()
                for part in manifest["bundle"]["parts"]
            )
            rebuilt_path = output_dir / "rebuilt.zip"
            rebuilt_path.write_bytes(rebuilt)
            with mirror.zipfile.ZipFile(rebuilt_path) as archive:
                self.assertIn("packages/com.tgc.sky.android-88.apk", archive.namelist())
            self.assertTrue((output_dir / "rebuild-zip.ps1").is_file())
            self.assertTrue((output_dir / "rebuild-zip.sh").is_file())

    def test_finalize_rejects_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            input_dir.mkdir()
            config_path = root / "config.json"
            probe_path = root / "probe.json"
            config_path.write_text(json.dumps(config()), encoding="utf-8")
            probe_path.write_text(
                json.dumps(mirror.build_probe_manifest(config(), details(version_code=99))),
                encoding="utf-8",
            )
            (input_dir / "com.tgc.sky.android-100.apk").write_bytes(b"newer")
            with self.assertRaisesRegex(RuntimeError, "differs from probe"):
                mirror.command_finalize(
                    SimpleNamespace(
                        config=config_path,
                        probe=probe_path,
                        input_dir=input_dir,
                        output_dir=root / "output",
                        repository="owner/Sky-mirror",
                    )
                )


if __name__ == "__main__":
    unittest.main()
