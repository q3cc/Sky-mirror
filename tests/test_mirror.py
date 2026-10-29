from __future__ import annotations

import importlib.util
import json
import os
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


PACKAGE = "com.tgc.sky.android"
GOOD_CERT = "a" * 64
OTHER_CERT = "b" * 64

# Stand-ins for the real tools. Behaviour is steered with FAKE_* environment variables.
FAKE_APKEEP = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("apkeep 1.1.0"); sys.exit(0)
Path(os.environ["FAKE_ARGV_LOG"]).write_text(" ".join(args))
ini = Path(args[args.index("-i") + 1]).read_text()
if "aas_token = good-token" not in ini or os.environ.get("FAKE_APKEEP_MODE") == "fail":
    print("Could not log in to Google Play. Please check your credentials and try again later.")
    sys.exit(0)  # apkeep really does exit 0 here
pkg = args[args.index("-a") + 1]
out = Path(args[-1])
opts = args[args.index("-o") + 1] if "-o" in args else ""
if "split_apk=true" in opts:
    d = out / pkg
    d.mkdir()
    (d / f"{pkg}.apk").write_bytes(b"base")
    (d / f"{pkg}.config.arm64_v8a.apk").write_bytes(os.environ.get("FAKE_SPLIT", "split").encode())
    (d / f"{pkg}.initial.apk").write_bytes(b"asset-pack")
else:
    (out / f"{pkg}.apk").write_bytes(b"base")
print(f"{pkg} downloaded successfully!")
'''

FAKE_AAPT2 = r'''#!/usr/bin/env python3
import os
print(f"package: name='com.tgc.sky.android' versionCode='{os.environ.get('FAKE_VC', '77')}' versionName='0.30.0' platformBuildVersionName='16'")
'''

FAKE_APKSIGNER = r'''#!/usr/bin/env python3
import sys
from pathlib import Path
data = Path(sys.argv[-1]).read_bytes()
digest = ("b" if data.startswith(b"evil") else "a") * 64
print(f"Signer #1 certificate DN: CN=test")
print(f"Signer #1 certificate SHA-256 digest: {digest}")
print("Source Stamp Signer certificate DN: CN=Google Play")
print(f"Source Stamp Signer certificate SHA-256 digest: {'c' * 64}")
'''

FAKE_GPLAYDL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
if os.environ.get("FAKE_GPLAYDL_MODE") == "fail":
    print("no accounts on this device yet", file=sys.stderr); sys.exit(1)
out = Path(args[args.index("--output") + 1])
pkg = args[1]
(out / f"{pkg}-77.apk").write_bytes(b"base")
(out / f"{pkg}-77-config.arm64_v8a.apk").write_bytes(b"split")
'''


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        tools = {
            "APKEEP": FAKE_APKEEP,
            "AAPT2": FAKE_AAPT2,
            "APKSIGNER": FAKE_APKSIGNER,
            "GPLAYDL": FAKE_GPLAYDL,
        }
        self.env = {
            "APKEEP_EMAIL": "mirror@example.com",
            "APKEEP_AAS_TOKEN": "good-token",
            "FAKE_ARGV_LOG": str(self.root / "argv.txt"),
        }
        for name, body in tools.items():
            path = bin_dir / name.lower()
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
            self.env[name] = str(path)
        self.saved = {key: os.environ.get(key) for key in [*self.env, "FAKE_APKEEP_MODE", "FAKE_GPLAYDL_MODE", "FAKE_SPLIT", "FAKE_VC", "GITHUB_OUTPUT", "GITHUB_ACTIONS"]}
        for key in self.saved:
            os.environ.pop(key, None)
        os.environ.update(self.env)

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def write_inputs(self, cfg: dict) -> tuple[Path, Path]:
        config_path = self.root / "config.json"
        probe_path = self.root / "probe.json"
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        probe_path.write_text(
            json.dumps(mirror.build_probe_manifest(cfg, details(version_code=77))), encoding="utf-8"
        )
        return config_path, probe_path

    def download(self, cfg: dict | None = None) -> tuple[int, dict, list[str]]:
        config_path, probe_path = self.write_inputs(cfg or config())
        out = self.root / "download"
        meta_path = self.root / "meta.json"
        result = mirror.command_download(
            SimpleNamespace(config=config_path, probe=probe_path, output_dir=out, meta=meta_path)
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return result, meta, sorted(path.name for path in out.iterdir())

    def test_apkeep_names_are_mapped_to_gplaydl_layout(self) -> None:
        self.assertEqual(mirror.apkeep_target_name(f"{PACKAGE}.apk", PACKAGE, 5), f"{PACKAGE}-5.apk")
        self.assertEqual(
            mirror.apkeep_target_name(f"{PACKAGE}.config.arm64_v8a.apk", PACKAGE, 5),
            f"{PACKAGE}-5-config.arm64_v8a.apk",
        )
        self.assertEqual(mirror.apkeep_target_name(f"{PACKAGE}.dm", PACKAGE, 5), f"{PACKAGE}-5.dm")
        self.assertEqual(
            mirror.apkeep_target_name(f"main.5.{PACKAGE}.obb", PACKAGE, 5), f"main.5.{PACKAGE}.obb"
        )
        with self.assertRaises(mirror.SourceError):
            mirror.apkeep_target_name("README.txt", PACKAGE, 5)

    def test_download_prefers_apkeep(self) -> None:
        result, meta, names = self.download()
        self.assertEqual(result, 0)
        self.assertEqual(meta["downloader"]["name"], "apkeep")
        self.assertEqual(meta["failedSources"], [])
        self.assertFalse(meta["playDigestsChecked"])
        self.assertEqual(meta["signingCertSha256"], GOOD_CERT)
        self.assertEqual(
            names,
            [f"{PACKAGE}-77-config.arm64_v8a.apk", f"{PACKAGE}-77-initial.apk", f"{PACKAGE}-77.apk"],
        )

    def test_token_is_not_passed_on_the_command_line(self) -> None:
        self.download()
        argv = (self.root / "argv.txt").read_text()
        self.assertNotIn("good-token", argv)
        self.assertIn("split_apk=true", argv)
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("apkeep-*.ini")), [])

    def test_apkeep_failure_falls_back_to_gplaydl(self) -> None:
        os.environ["FAKE_APKEEP_MODE"] = "fail"
        result, meta, names = self.download()
        self.assertEqual(result, 0)
        self.assertEqual(meta["downloader"]["name"], "gplaydl")
        self.assertTrue(meta["playDigestsChecked"])
        self.assertEqual(len(meta["failedSources"]), 1)
        self.assertIn("apkeep", meta["failedSources"][0])
        self.assertEqual(names, [f"{PACKAGE}-77-config.arm64_v8a.apk", f"{PACKAGE}-77.apk"])

    def test_mixed_signers_reject_apkeep_download(self) -> None:
        os.environ["FAKE_SPLIT"] = "evil"
        _, meta, _ = self.download()
        self.assertEqual(meta["downloader"]["name"], "gplaydl")
        self.assertIn("signed by", meta["failedSources"][0])

    def test_pinned_certificate_must_match(self) -> None:
        cfg = config()
        cfg["app"]["signingCertSha256"] = OTHER_CERT
        os.environ["FAKE_GPLAYDL_MODE"] = "fail"
        config_path, probe_path = self.write_inputs(cfg)
        with self.assertRaisesRegex(RuntimeError, "every download source failed"):
            mirror.command_download(
                SimpleNamespace(
                    config=config_path, probe=probe_path,
                    output_dir=self.root / "download", meta=self.root / "meta.json",
                )
            )

    def test_pinned_certificate_accepts_match(self) -> None:
        cfg = config()
        cfg["app"]["signingCertSha256"] = GOOD_CERT.upper()
        _, meta, _ = self.download(cfg)
        self.assertTrue(meta["signingCertPinned"])

    def test_probe_reads_version_from_apkeep_base_apk(self) -> None:
        os.environ["FAKE_VC"] = "4242"
        found, source, failures = mirror.fetch_play_details(config())
        self.assertEqual(source, "apkeep")
        self.assertEqual(failures, [])
        self.assertEqual(found["versionCode"], 4242)
        self.assertNotIn("split_apk", (self.root / "argv.txt").read_text())

    def test_probe_falls_back_when_apkeep_is_not_configured(self) -> None:
        os.environ.pop("APKEEP_AAS_TOKEN")
        original = mirror.FETCH_DETAILS["gplaydl"]
        mirror.FETCH_DETAILS["gplaydl"] = lambda cfg: details(version_code=9)
        try:
            found, source, failures = mirror.fetch_play_details(config())
        finally:
            mirror.FETCH_DETAILS["gplaydl"] = original
        self.assertEqual((source, found["versionCode"]), ("gplaydl", 9))
        self.assertIn("APKEEP_AAS_TOKEN", failures[0])

    def test_finalize_records_downloader_and_signer(self) -> None:
        _, meta, _ = self.download()
        config_path, probe_path = self.write_inputs(config())
        output_dir = self.root / "release"
        mirror.command_finalize(
            SimpleNamespace(
                config=config_path, probe=probe_path, input_dir=self.root / "download",
                output_dir=output_dir, repository="owner/Sky-mirror",
                download_meta=self.root / "meta.json",
            )
        )
        manifest = json.loads((output_dir / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["download"]["downloader"]["name"], "apkeep")
        self.assertFalse(manifest["verification"]["playDigestsCheckedByDownloader"])
        self.assertEqual(manifest["verification"]["signingCertSha256"], GOOD_CERT)
        notes = (output_dir / "release-notes.md").read_text(encoding="utf-8")
        self.assertIn("Downloader: `apkeep`", notes)
        self.assertIn(GOOD_CERT, notes)

    def test_config_rejects_unknown_source_and_bad_cert(self) -> None:
        for mutate in (
            lambda c: c["download"].__setitem__("sources", ["apkeep", "nope"]),
            lambda c: c["download"].__setitem__("sources", []),
            lambda c: c["app"].__setitem__("signingCertSha256", "xyz"),
        ):
            cfg = config()
            mutate(cfg)
            path = self.root / "bad.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(ValueError):
                mirror.load_config(path)


if __name__ == "__main__":
    unittest.main()
