#!/usr/bin/env python3
"""Probe Google Play and prepare verifiable GitHub Release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


PACKAGE_SUFFIXES = {".apk", ".obb", ".dm"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path)
    if config.get("schemaVersion") != 1:
        raise ValueError("mirror-config.json schemaVersion must be 1")

    app = config.get("app") or {}
    download = config.get("download") or {}
    release = config.get("release") or {}
    package = app.get("packageName")
    if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
        raise ValueError("app.packageName is not a valid Android package name")
    if download.get("architecture") not in {"arm64", "armv7", "x86", "x86_64", "tv"}:
        raise ValueError("download.architecture is not supported by gplaydl")
    if not isinstance(download.get("locales", []), list):
        raise ValueError("download.locales must be an array")
    if not release.get("tagPrefix") or not release.get("bundleName"):
        raise ValueError("release.tagPrefix and release.bundleName are required")
    return config


def safe_tag_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    value = re.sub(r"-+", "-", value).strip(".-")
    return value or "unknown"


def make_release_tag(config: dict[str, Any], version_name: str, version_code: int) -> str:
    prefix = safe_tag_component(str(config["release"]["tagPrefix"]))
    return f"{prefix}-{safe_tag_component(version_name)}-vc{version_code}"


def release_identity(document: dict[str, Any]) -> tuple[Any, ...]:
    app = document.get("app") or {}
    download = document.get("download") or {}
    return (
        document.get("source"),
        app.get("packageName"),
        app.get("versionCode"),
        download.get("architecture"),
        tuple(download.get("locales") or []),
        bool(download.get("includeSplits", True)),
        bool(download.get("includeExtras", True)),
    )


def write_github_outputs(values: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8", newline="\n") as handle:
        for key, raw_value in values.items():
            value = str(raw_value).lower() if isinstance(raw_value, bool) else str(raw_value)
            if "\n" in value:
                marker = f"SKY_MIRROR_{key.upper()}_EOF"
                handle.write(f"{key}<<{marker}\n{value}\n{marker}\n")
            else:
                handle.write(f"{key}={value}\n")


def gplaydl_version() -> str:
    try:
        import gplaydl

        return str(gplaydl.__version__)
    except (ImportError, AttributeError):
        return "unknown"


def fetch_play_details(config: dict[str, Any]) -> dict[str, Any]:
    try:
        from gplaydl.api import AuthExpiredError, PlayAPIError, get_details
        from gplaydl.auth import DispenserError, ensure_auth
    except ImportError as exc:
        raise RuntimeError("gplaydl is not installed; run pip install -r requirements.txt") from exc

    package = config["app"]["packageName"]
    arch = config["download"]["architecture"]
    email = os.environ.get("GPLAYDL_EMAIL") or None
    try:
        auth = ensure_auth(arch=arch, email=email)
        if not auth:
            raise RuntimeError("gplaydl could not obtain a Google Play token")
        try:
            details = get_details(package, auth)
        except AuthExpiredError:
            auth = ensure_auth(arch=arch, force_refresh=True, email=email)
            if not auth:
                raise RuntimeError("gplaydl could not refresh the Google Play token")
            details = get_details(package, auth)
    except (DispenserError, PlayAPIError) as exc:
        raise RuntimeError(f"Google Play probe failed: {exc}") from exc

    if not details.version_code:
        raise RuntimeError(f"Google Play did not return a version code for {package}")
    return {
        "title": details.title or config["app"]["title"],
        "packageName": details.package or package,
        "versionName": details.version_string or str(details.version_code),
        "versionCode": int(details.version_code),
        "developer": details.developer or None,
        "playUrl": details.play_url or config["app"]["playUrl"],
    }


def build_probe_manifest(config: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    version_name = str(details["versionName"])
    version_code = int(details["versionCode"])
    return {
        "schemaVersion": 1,
        "source": "google-play",
        "app": details,
        "download": {
            "architecture": config["download"]["architecture"],
            "locales": config["download"].get("locales", []),
            "includeSplits": bool(config["download"].get("includeSplits", True)),
            "includeExtras": bool(config["download"].get("includeExtras", True)),
            "includeDexMetadata": bool(config["download"].get("includeDexMetadata", False)),
        },
        "probe": {
            "observedAt": utc_now(),
            "downloader": {"name": "gplaydl", "version": gplaydl_version()},
        },
        "release": {
            "tag": make_release_tag(config, version_name, version_code),
            "title": f"Sky: Children of the Light {version_name} (vc {version_code})",
        },
    }


def command_probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    manifest = build_probe_manifest(config, fetch_play_details(config))
    previous = load_json(args.previous) if args.previous and args.previous.is_file() else None
    changed = previous is None or release_identity(previous) != release_identity(manifest)
    should_release = bool(args.force or changed)
    if args.force:
        reason = "forced"
    elif previous is None:
        reason = "first-release"
    elif changed:
        reason = "new-version"
    else:
        reason = "latest-release-already-matches"

    write_json(args.output, manifest)
    outputs = {
        "should_release": should_release,
        "release_tag": manifest["release"]["tag"],
        "release_title": manifest["release"]["title"],
        "version_name": manifest["app"]["versionName"],
        "version_code": manifest["app"]["versionCode"],
        "skip_reason": reason,
    }
    write_github_outputs(outputs)
    print(json.dumps(outputs, ensure_ascii=False))
    return 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_kind(name: str, package: str, version_code: int) -> str:
    if name == f"{package}-{version_code}.apk":
        return "base-apk"
    if name.endswith(".obb"):
        return "obb"
    if name.endswith(".dm"):
        return "dex-metadata"
    return "split-apk" if name.endswith(".apk") else "file"


def release_url(repository: str, tag: str, name: str) -> str:
    return f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{quote(name)}"


def create_bundle(
    files: list[Path], output: Path, max_bytes: int, package: str
) -> dict[str, Any]:
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes > max_bytes:
        return {
            "created": False,
            "reason": "source-files-exceed-bundle-limit",
            "sourceBytes": total_bytes,
            "limitBytes": max_bytes,
        }
    install_text = (
        "These are the original files delivered by Google Play.\n"
        "Do not install only the base APK when split APKs are present.\n\n"
        "Example:\n"
        f"  adb install-multiple packages/{package}-*.apk\n"
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("INSTALL.txt", install_text)
        for path in files:
            archive.write(path, f"packages/{path.name}")
    size = output.stat().st_size
    if size > max_bytes:
        output.unlink()
        return {
            "created": False,
            "reason": "bundle-output-exceeds-limit",
            "sourceBytes": total_bytes,
            "limitBytes": max_bytes,
        }
    return {"created": True, "size": size, "sha256": sha256_file(output)}


def build_release_notes(manifest: dict[str, Any], repository: str) -> str:
    app = manifest["app"]
    download = manifest["download"]
    bundle = manifest["bundle"]
    lines = [
        f"# Sky: Children of the Light {app['versionName']}",
        "",
        "## 下载",
        "",
        f"- Google Play versionCode: `{app['versionCode']}`",
        f"- 架构: `{download['architecture']}`",
        f"- 原始文件数: `{len(manifest['artifacts'])}`",
    ]
    if bundle.get("created"):
        lines.append(f"- 完整包: [`{bundle['name']}`]({bundle['url']})")
    else:
        lines.append("- 完整包超过单文件发布限制，请使用 `download-index.json` 下载全部原始文件。")
    lines.extend(
        [
            f"- 稳定下载索引: https://github.com/{repository}/releases/latest/download/download-index.json",
            "",
            "## 校验与安装",
            "",
            "文件由 Google Play 直接交付，镜像不修改、不合并、不重新签名。",
            "请使用 `SHA256SUMS.txt` 校验完整性；存在 split APK 时需与 base APK 一起安装。",
            "",
            "## 来源",
            "",
            f"- Package: `{app['packageName']}`",
            f"- Google Play: {app['playUrl']}",
            "- Downloader: `gplaydl`",
            "",
            "This is an unofficial mirror and is not affiliated with Google or thatgamecompany.",
            "",
        ]
    )
    return "\n".join(lines)


def command_finalize(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    probe = load_json(args.probe)
    package = str(probe["app"]["packageName"])
    version_code = int(probe["app"]["versionCode"])
    tag = str(probe["release"]["tag"])
    expected_base = f"{package}-{version_code}.apk"

    source_files = sorted(
        path for path in args.input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in PACKAGE_SUFFIXES
    )
    if not source_files:
        raise RuntimeError(f"no Google Play package files found in {args.input_dir}")
    if not any(path.name == expected_base for path in source_files):
        found = [path.name for path in source_files if re.fullmatch(re.escape(package) + r"-\d+\.apk", path.name)]
        raise RuntimeError(f"downloaded version differs from probe; expected {expected_base}, found {found or 'no base APK'}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    asset_limit = int(config["release"].get("assetMaxBytes", 2147483647))
    artifacts: list[dict[str, Any]] = []
    copied_files: list[Path] = []
    for source in source_files:
        size = source.stat().st_size
        if size > asset_limit:
            raise RuntimeError(f"{source.name} exceeds the configured GitHub Release asset limit")
        destination = args.output_dir / source.name
        shutil.copy2(source, destination)
        copied_files.append(destination)
        artifacts.append(
            {
                "name": destination.name,
                "kind": artifact_kind(destination.name, package, version_code),
                "size": size,
                "sha256": sha256_file(destination),
                "url": release_url(args.repository, tag, destination.name),
            }
        )

    bundle_name = str(config["release"]["bundleName"])
    bundle = create_bundle(
        copied_files,
        args.output_dir / bundle_name,
        int(config["release"].get("bundleMaxBytes", 1900000000)),
        package,
    )
    if bundle.get("created"):
        bundle.update({"name": bundle_name, "url": release_url(args.repository, tag, bundle_name)})

    checksummed = list(artifacts)
    if bundle.get("created"):
        checksummed.append({"name": bundle["name"], "sha256": bundle["sha256"], "size": bundle["size"]})
    with (args.output_dir / "SHA256SUMS.txt").open("w", encoding="ascii", newline="\n") as handle:
        for item in checksummed:
            handle.write(f"{item['sha256']}  {item['name']}\n")

    manifest = dict(probe)
    manifest["release"] = {
        **probe["release"],
        "publishedFrom": args.repository,
        "preparedAt": utc_now(),
    }
    manifest["artifacts"] = artifacts
    manifest["bundle"] = bundle
    manifest["verification"] = {
        "algorithm": "sha256",
        "checksumsAsset": "SHA256SUMS.txt",
        "downloadVerifiedByGplaydl": True,
    }
    write_json(args.output_dir / "release-manifest.json", manifest)

    index = {
        "schemaVersion": 1,
        "app": manifest["app"],
        "release": manifest["release"],
        "bundle": bundle,
        "assets": artifacts,
        "checksums": release_url(args.repository, tag, "SHA256SUMS.txt"),
        "manifest": release_url(args.repository, tag, "release-manifest.json"),
        "latestIndex": f"https://github.com/{args.repository}/releases/latest/download/download-index.json",
    }
    write_json(args.output_dir / "download-index.json", index)
    (args.output_dir / "release-notes.md").write_text(
        build_release_notes(manifest, args.repository), encoding="utf-8", newline="\n"
    )
    write_github_outputs(
        {
            "release_tag": tag,
            "release_title": probe["release"]["title"],
            "bundle_created": bool(bundle.get("created")),
            "artifact_count": len(artifacts),
        }
    )
    print(f"Prepared {len(artifacts)} Google Play files for {tag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe", help="probe Google Play and compare the latest release")
    probe.add_argument("--config", type=Path, default=Path("mirror-config.json"))
    probe.add_argument("--previous", type=Path)
    probe.add_argument("--output", type=Path, default=Path("probe-manifest.json"))
    probe.add_argument("--force", action="store_true")
    probe.set_defaults(func=command_probe)

    finalize = subparsers.add_parser("finalize", help="verify downloads and build release metadata")
    finalize.add_argument("--config", type=Path, default=Path("mirror-config.json"))
    finalize.add_argument("--probe", type=Path, required=True)
    finalize.add_argument("--input-dir", type=Path, required=True)
    finalize.add_argument("--output-dir", type=Path, required=True)
    finalize.add_argument("--repository", required=True)
    finalize.set_defaults(func=command_finalize)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
