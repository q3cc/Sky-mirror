#!/usr/bin/env python3
"""Probe Google Play and prepare verifiable GitHub Release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    domestic_mirror_url = release.get("domesticMirrorUrl")
    if domestic_mirror_url is not None and (
        not isinstance(domestic_mirror_url, str)
        or not domestic_mirror_url.startswith("https://")
    ):
        raise ValueError("release.domesticMirrorUrl must be an HTTPS URL")
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
    files: list[Path], output: Path, part_max_bytes: int, package: str
) -> dict[str, Any]:
    if part_max_bytes <= 0:
        raise ValueError("release.bundlePartMaxBytes must be greater than zero")
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
    checksum = sha256_file(output)
    if size <= part_max_bytes:
        return {
            "created": True,
            "singleFile": True,
            "size": size,
            "sha256": checksum,
            "assets": [{"name": output.name, "size": size, "sha256": checksum}],
            "parts": [],
        }

    parts: list[dict[str, Any]] = []
    with output.open("rb") as source:
        part_number = 1
        while source.tell() < size:
            part_path = output.with_name(f"{output.name}.{part_number:03d}")
            digest = hashlib.sha256()
            written = 0
            with part_path.open("wb") as destination:
                while written < part_max_bytes:
                    chunk = source.read(min(8 * 1024 * 1024, part_max_bytes - written))
                    if not chunk:
                        break
                    destination.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            parts.append({"name": part_path.name, "size": written, "sha256": digest.hexdigest()})
            part_number += 1
    output.unlink()

    powershell_path = output.parent / "rebuild-zip.ps1"
    powershell_path.write_text(
        f'param([string]$Output = "{output.name}")\n'
        '$ErrorActionPreference = "Stop"\n'
        f'$parts = Get-ChildItem -LiteralPath $PSScriptRoot -Filter "{output.name}.???" | Sort-Object Name\n'
        'if ($parts.Count -eq 0) { throw "No ZIP parts found." }\n'
        '$target = Join-Path $PSScriptRoot $Output\n'
        '$writer = [IO.File]::Create($target)\n'
        'try {\n'
        '  foreach ($part in $parts) {\n'
        '    $reader = [IO.File]::OpenRead($part.FullName)\n'
        '    try { $reader.CopyTo($writer) } finally { $reader.Dispose() }\n'
        '  }\n'
        '} finally { $writer.Dispose() }\n'
        '$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()\n'
        f'$expected = "{checksum}"\n'
        'if ($actual -ne $expected) { Remove-Item -LiteralPath $target; throw "ZIP checksum mismatch." }\n'
        'Write-Host "Created $target (SHA-256 verified)"\n',
        encoding="utf-8",
        newline="\n",
    )
    shell_path = output.parent / "rebuild-zip.sh"
    shell_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cd "$(dirname "$0")"\n'
        f"cat {output.name}.[0-9][0-9][0-9] > {output.name}\n"
        f"printf '%s  %s\\n' '{checksum}' '{output.name}' | sha256sum --check -\n",
        encoding="utf-8",
        newline="\n",
    )
    scripts = [
        {"name": path.name, "size": path.stat().st_size, "sha256": sha256_file(path)}
        for path in (powershell_path, shell_path)
    ]
    return {
        "created": True,
        "singleFile": False,
        "size": size,
        "sha256": checksum,
        "assets": parts + scripts,
        "parts": parts,
        "reassembly": {"windows": powershell_path.name, "linuxAndroid": shell_path.name},
    }


def build_release_notes(manifest: dict[str, Any], repository: str) -> str:
    app = manifest["app"]
    download = manifest["download"]
    bundle = manifest["bundle"]
    release = manifest["release"]
    release_page = (
        f"https://github.com/{repository}/releases/tag/"
        f"{quote(release['tag'], safe='')}"
    )
    domestic_mirror_url = str(release.get("domesticMirrorUrl") or "").strip()
    lines = [
        "# Sky Android 镜像更新",
        "",
        f"本次 Release 同步了 **Sky: Children of the Light** `{app['versionName']}` "
        f"（versionCode `{app['versionCode']}`）的 Google Play 安装组件。",
        "",
        "## 下载",
        "",
        f"- GitHub Release: {release_page}",
        f"- 稳定下载索引: https://github.com/{repository}/releases/latest/download/download-index.json",
    ]
    if domestic_mirror_url:
        lines.append(f"- 国内网盘分流（123云盘）: {domestic_mirror_url}")
        lines.extend(
            [
                "",
                "国内网盘不属于 GitHub Actions 自动发布流程。通过网盘下载后，"
                "请选择相同版本，并用本 Release 的 `SHA256SUMS.txt` 校验文件。",
            ]
        )
    lines.append("")

    lines.extend(
        [
            "## 版本与文件",
            "",
            "| 项目 | 内容 |",
            "|---|---|",
            f"| versionName | `{app['versionName']}` |",
            f"| versionCode | `{app['versionCode']}` |",
            f"| 架构 | `{download['architecture']}` |",
            f"| Google Play 原始文件 | `{len(manifest['artifacts'])}` 个 |",
        ]
    )
    if bundle["singleFile"]:
        lines.append(f"| 完整 ZIP | [`{bundle['name']}`]({bundle['url']}) |")
    else:
        lines.append(f"| 完整 ZIP 大小 | `{bundle['size']}` 字节 |")
        lines.append(f"| ZIP 分片 | `{len(bundle['parts'])}` 个，每片不超过 1 GB |")
        part_names = "、".join(f"`{part['name']}`" for part in bundle["parts"])
        lines.extend(["", f"分片文件：{part_names}"])

    lines.extend(
        [
            "",
            "## 还原与安装",
            "",
        ]
    )
    if bundle["singleFile"]:
        lines.append(f"下载并解压 `{bundle['name']}`。")
    else:
        lines.extend(
            [
                "下载全部 ZIP 分片和对应的还原脚本，放在同一目录运行：",
                "",
                "```powershell",
                ".\\rebuild-zip.ps1",
                "```",
                "",
                "```bash",
                "bash rebuild-zip.sh",
                "```",
                "",
                "还原脚本会生成 `sky-android-arm64.zip`，并自动核对完整 ZIP 的 SHA-256。",
            ]
        )
    lines.extend(
        [
            "",
            "解压 ZIP 后，用 ADB 一次安装 base APK 和全部 split APK：",
            "",
            "```bash",
            f"adb install-multiple packages/{app['packageName']}-*.apk",
            "```",
            "",
            "## 校验与来源",
            "",
            "ZIP 内组件由 Google Play 直接交付，本镜像不修改、不合并，也不重新签名。",
            "`gplaydl` 会按 Google Play 交付清单校验下载内容；本 Release 另附 `SHA256SUMS.txt`、`release-manifest.json` 和 `download-index.json`。",
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
    part_limit = int(config["release"].get("bundlePartMaxBytes", 1000000000))
    if part_limit > asset_limit:
        raise ValueError("release.bundlePartMaxBytes cannot exceed release.assetMaxBytes")
    artifacts: list[dict[str, Any]] = []
    for source in source_files:
        size = source.stat().st_size
        artifacts.append(
            {
                "name": source.name,
                "pathInZip": f"packages/{source.name}",
                "kind": artifact_kind(source.name, package, version_code),
                "size": size,
                "sha256": sha256_file(source),
            }
        )

    bundle_name = str(config["release"]["bundleName"])
    bundle = create_bundle(
        source_files,
        args.output_dir / bundle_name,
        part_limit,
        package,
    )
    bundle["name"] = bundle_name
    for asset in bundle["assets"]:
        asset["url"] = release_url(args.repository, tag, asset["name"])
    for part in bundle["parts"]:
        part["url"] = release_url(args.repository, tag, part["name"])
    if bundle["singleFile"]:
        bundle["url"] = release_url(args.repository, tag, bundle_name)

    with (args.output_dir / "SHA256SUMS.txt").open("w", encoding="ascii", newline="\n") as handle:
        for item in bundle["assets"]:
            handle.write(f"{item['sha256']}  {item['name']}\n")
        if not bundle["singleFile"]:
            handle.write("# Complete ZIP after reassembly:\n")
            handle.write(f"{bundle['sha256']}  {bundle['name']}\n")

    manifest = dict(probe)
    manifest["release"] = {
        **probe["release"],
        "publishedFrom": args.repository,
        "preparedAt": utc_now(),
    }
    domestic_mirror_url = str(config["release"].get("domesticMirrorUrl") or "").strip()
    if domestic_mirror_url:
        manifest["release"]["domesticMirrorUrl"] = domestic_mirror_url
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
        "assets": bundle["assets"],
        "components": artifacts,
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
