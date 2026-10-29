#!/usr/bin/env python3
"""Probe Google Play and prepare verifiable GitHub Release assets."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote


PACKAGE_SUFFIXES = {".apk", ".obb", ".dm"}
DOWNLOAD_SOURCES = ("apkeep", "gplaydl")
DEFAULT_SOURCES = ["apkeep", "gplaydl"]


class SourceError(RuntimeError):
    """One download source failed; the next source may still work."""


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
    sources = download.get("sources", DEFAULT_SOURCES)
    if (
        not isinstance(sources, list)
        or not sources
        or any(source not in DOWNLOAD_SOURCES for source in sources)
        or len(set(sources)) != len(sources)
    ):
        raise ValueError(f"download.sources must be a non-empty list drawn from {list(DOWNLOAD_SOURCES)}")
    cert = app.get("signingCertSha256")
    if cert is not None and (not isinstance(cert, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", cert)):
        raise ValueError("app.signingCertSha256 must be a 64-character hex SHA-256 digest")
    return config


def download_sources(config: dict[str, Any]) -> list[str]:
    return list(config["download"].get("sources", DEFAULT_SOURCES))


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


def append_step_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(text.rstrip("\n") + "\n")


def warn(message: str) -> None:
    # Shows up as an annotation on the workflow run.
    print(f"::warning::{message}" if os.environ.get("GITHUB_ACTIONS") else f"warning: {message}", file=sys.stderr)


# --- External tools -------------------------------------------------------


def find_executable(name: str, env_var: str) -> str:
    override = os.environ.get(env_var)
    if override:
        return override
    sibling = Path(sys.executable).with_name(name)
    if sibling.is_file():
        return str(sibling)
    found = shutil.which(name)
    if found:
        return found
    raise SourceError(f"{name} was not found; set {env_var} or add it to PATH")


def find_android_tool(name: str, env_var: str) -> str:
    override = os.environ.get(env_var)
    if override:
        return override
    for sdk_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(sdk_var)
        if not sdk:
            continue
        candidates = [path for path in (Path(sdk) / "build-tools").glob(f"*/{name}") if path.is_file()]

        def version_key(path: Path) -> tuple[int, ...]:
            return tuple(int(part) for part in re.findall(r"\d+", path.parent.name))

        if candidates:
            return str(max(candidates, key=version_key))
    found = shutil.which(name)
    if found:
        return found
    raise SourceError(f"{name} was not found; install Android build-tools or set {env_var}")


def run_tool(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"{Path(command[0]).name} timed out after {timeout}s") from exc
    except OSError as exc:
        raise SourceError(f"could not run {command[0]}: {exc}") from exc


def tail(text: str, lines: int = 5) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:] if line.strip())


def read_badging(apk: Path) -> dict[str, Any]:
    result = run_tool([find_android_tool("aapt2", "AAPT2"), "dump", "badging", str(apk)], timeout=120)
    match = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", result.stdout)
    if result.returncode != 0 or not match:
        raise SourceError(f"aapt2 could not read {apk.name}: {tail(result.stderr or result.stdout)}")
    return {"packageName": match.group(1), "versionCode": int(match.group(2)), "versionName": match.group(3)}


def signer_digests(apk: Path) -> set[str]:
    result = run_tool(
        [find_android_tool("apksigner", "APKSIGNER"), "verify", "--print-certs", str(apk)], timeout=600
    )
    # Only "Signer #N" lines are the developer's certificates. Play-delivered APKs may also
    # carry a "Source Stamp Signer" line (Google Play's own stamp), which is not a signer.
    digests = set(re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-f]{64})$", result.stdout, re.M))
    if result.returncode != 0 or not digests:
        raise SourceError(f"apksigner rejected {apk.name}: {tail(result.stderr or result.stdout)}")
    return digests


def verify_signatures(files: list[Path], expected: str | None) -> str:
    """Every APK must be signed by the same single certificate (and match the pin, if set)."""
    apks = [path for path in files if path.suffix.lower() == ".apk"]
    if not apks:
        raise SourceError("no APK files to verify")
    signer: str | None = None
    for apk in apks:
        digests = signer_digests(apk)
        if len(digests) != 1:
            raise SourceError(f"{apk.name} has {len(digests)} signers; expected exactly one")
        (digest,) = digests
        if signer is None:
            signer = digest
        elif digest != signer:
            raise SourceError(f"{apk.name} is signed by {digest}, other files by {signer}")
    assert signer is not None
    if expected and signer != expected.lower():
        raise SourceError(f"signing certificate {signer} does not match app.signingCertSha256 {expected.lower()}")
    return signer


def package_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in PACKAGE_SUFFIXES
    )


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


# --- apkeep (own Google account, AAS token) --------------------------------


def apkeep_supported(config: dict[str, Any]) -> None:
    download = config["download"]
    # apkeep's default device profile (px_9a) is arm64 and it has no extra-locale option.
    if download["architecture"] != "arm64":
        raise SourceError("apkeep is only set up for arm64 here")
    if download.get("locales"):
        raise SourceError("apkeep cannot download extra locale splits")


@contextlib.contextmanager
def apkeep_ini() -> Iterator[Path]:
    email = os.environ.get("APKEEP_EMAIL", "").strip()
    token = os.environ.get("APKEEP_AAS_TOKEN", "").strip()
    if not email or not token:
        raise SourceError("APKEEP_EMAIL or APKEEP_AAS_TOKEN is not set")
    # Pass the token through a private file so it never shows up in the process list.
    handle, name = tempfile.mkstemp(prefix="apkeep-", suffix=".ini")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as ini:
            ini.write(f"[google]\nemail = {email}\naas_token = {token}\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def run_apkeep_once(package: str, output_dir: Path, options: list[str]) -> None:
    with apkeep_ini() as ini:
        command = [find_executable("apkeep", "APKEEP"), "-a", package, "-d", "google-play", "-i", str(ini)]
        if options:
            command += ["-o", ",".join(options)]
        command.append(str(output_dir))
        result = run_tool(command, timeout=3600)
    output = f"{result.stdout}\n{result.stderr}"
    # apkeep can exit 0 after a failed download, and without a terminal it hides the
    # reason (it is printed through the progress bar), so check for the success line.
    if result.returncode != 0 or "downloaded successfully" not in output:
        got = sum(1 for path in output_dir.rglob("*") if path.is_file())
        raise SourceError(f"apkeep failed ({got} files written): {tail(output)}")


def run_apkeep(package: str, output_dir: Path, options: list[str]) -> None:
    """Retry with an empty directory each time.

    apkeep's own retries are no-ops for split downloads: after a partial first
    attempt the split directory exists, so it skips with "directory already exists".
    """
    attempts = max(1, int(os.environ.get("APKEEP_ATTEMPTS", "3")))
    delay = int(os.environ.get("APKEEP_RETRY_DELAY", "20"))
    for attempt in range(1, attempts + 1):
        reset_dir(output_dir)
        try:
            run_apkeep_once(package, output_dir, options)
            return
        except SourceError as exc:
            if attempt == attempts:
                raise SourceError(f"{exc} (after {attempts} attempts)") from exc
            warn(f"apkeep attempt {attempt}/{attempts} failed, retrying in {delay}s: {exc}")
            time.sleep(delay * attempt)


def apkeep_version() -> str:
    try:
        result = run_tool([find_executable("apkeep", "APKEEP"), "--version"], timeout=30)
    except SourceError:
        return "unknown"
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
    return match.group(1) if match else "unknown"


def fetch_details_apkeep(config: dict[str, Any]) -> dict[str, Any]:
    """apkeep has no metadata call, so fetch only the base APK (~50 MB) and read its manifest."""
    apkeep_supported(config)
    package = config["app"]["packageName"]
    with tempfile.TemporaryDirectory(prefix="apkeep-probe-") as temp:
        run_apkeep(package, Path(temp), [])
        base = next(Path(temp).rglob(f"{package}.apk"), None)
        if base is None:
            raise SourceError("apkeep did not produce a base APK")
        badging = read_badging(base)
    if badging["packageName"] != package:
        raise SourceError(f"apkeep returned {badging['packageName']}, expected {package}")
    return {
        "title": config["app"]["title"],
        "packageName": package,
        "versionName": badging["versionName"] or str(badging["versionCode"]),
        "versionCode": badging["versionCode"],
        "developer": None,
        "playUrl": config["app"]["playUrl"],
    }


def apkeep_target_name(name: str, package: str, version_code: int) -> str:
    """Rename apkeep output to the gplaydl layout used by finalize and earlier releases."""
    if name == f"{package}.apk":
        return f"{package}-{version_code}.apk"
    if name == f"{package}.dm":
        return f"{package}-{version_code}.dm"
    if name.startswith(f"{package}.") and name.endswith(".apk"):
        return f"{package}-{version_code}-{name[len(package) + 1:-4]}.apk"
    if name.endswith(".obb"):
        return name  # apkeep and gplaydl both use main|patch.<vc>.<package>.obb
    raise SourceError(f"unexpected file from apkeep: {name}")


def download_apkeep(config: dict[str, Any], output_dir: Path) -> None:
    apkeep_supported(config)
    download = config["download"]
    package = config["app"]["packageName"]
    options = []
    if download.get("includeSplits", True):
        options.append("split_apk=true")
    if download.get("includeExtras", True):
        options.append("include_additional_files=true")
    if download.get("includeDexMetadata", False):
        options.append("include_dex_metadata=true")

    staging = output_dir.with_name(output_dir.name + ".apkeep")
    reset_dir(staging)
    try:
        run_apkeep(package, staging, options)
        files = [path for path in staging.rglob("*") if path.is_file()]
        base = next((path for path in files if path.name == f"{package}.apk"), None)
        if base is None:
            raise SourceError("apkeep did not produce a base APK")
        version_code = read_badging(base)["versionCode"]
        for path in files:
            target = output_dir / apkeep_target_name(path.name, package, version_code)
            if target.exists():
                raise SourceError(f"apkeep produced {target.name} twice")
            path.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --- gplaydl (token dispenser) --------------------------------------------


def gplaydl_version() -> str:
    try:
        import gplaydl

        return str(gplaydl.__version__)
    except (ImportError, AttributeError):
        return "unknown"


def fetch_details_gplaydl(config: dict[str, Any]) -> dict[str, Any]:
    try:
        from gplaydl.api import AuthExpiredError, PlayAPIError, get_details
        from gplaydl.auth import DispenserError, ensure_auth
    except ImportError as exc:
        raise SourceError("gplaydl is not installed; run pip install -r requirements.txt") from exc

    package = config["app"]["packageName"]
    arch = config["download"]["architecture"]
    email = os.environ.get("GPLAYDL_EMAIL") or None
    try:
        auth = ensure_auth(arch=arch, email=email)
        if not auth:
            raise SourceError("gplaydl could not obtain a Google Play token")
        try:
            details = get_details(package, auth)
        except AuthExpiredError:
            auth = ensure_auth(arch=arch, force_refresh=True, email=email)
            if not auth:
                raise SourceError("gplaydl could not refresh the Google Play token")
            details = get_details(package, auth)
    except (DispenserError, PlayAPIError) as exc:
        raise SourceError(f"gplaydl probe failed: {exc}") from exc

    if not details.version_code:
        raise SourceError(f"Google Play did not return a version code for {package}")
    return {
        "title": details.title or config["app"]["title"],
        "packageName": details.package or package,
        "versionName": details.version_string or str(details.version_code),
        "versionCode": int(details.version_code),
        "developer": details.developer or None,
        "playUrl": details.play_url or config["app"]["playUrl"],
    }


def download_gplaydl(config: dict[str, Any], output_dir: Path) -> None:
    download = config["download"]
    command = [
        find_executable("gplaydl", "GPLAYDL"), "download", config["app"]["packageName"],
        "--output", str(output_dir), "--arch", download["architecture"],
    ]
    if os.environ.get("GPLAYDL_EMAIL"):
        command += ["--email", os.environ["GPLAYDL_EMAIL"]]
    if not download.get("includeSplits", True):
        command.append("--no-splits")
    if not download.get("includeExtras", True):
        command.append("--no-extras")
    if download.get("includeDexMetadata", False):
        command.append("--dm")
    if download.get("locales"):
        command += ["--locale", ",".join(download["locales"])]
    result = run_tool(command, timeout=3600)
    if result.returncode != 0:
        raise SourceError(f"gplaydl failed: {tail(result.stdout + result.stderr)}")


FETCH_DETAILS = {"apkeep": fetch_details_apkeep, "gplaydl": fetch_details_gplaydl}
DOWNLOADERS = {"apkeep": download_apkeep, "gplaydl": download_gplaydl}
TOOL_VERSIONS = {"apkeep": apkeep_version, "gplaydl": gplaydl_version}
# gplaydl checks every file against the SHA-256 in Play's delivery response; apkeep does not.
CHECKS_PLAY_DIGESTS = {"apkeep": False, "gplaydl": True}


def fetch_play_details(config: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    """Try each source in order. Returns (details, source used, failures before it)."""
    failures: list[str] = []
    for source in download_sources(config):
        try:
            return FETCH_DETAILS[source](config), source, failures
        except SourceError as exc:
            failures.append(f"{source}: {exc}")
            warn(f"probe via {source} failed: {exc}")
    raise RuntimeError("every probe source failed: " + "; ".join(failures))


def build_probe_manifest(
    config: dict[str, Any], details: dict[str, Any], source: str = "gplaydl"
) -> dict[str, Any]:
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
            "downloader": {"name": source, "version": TOOL_VERSIONS[source]()},
        },
        "release": {
            "tag": make_release_tag(config, version_name, version_code),
            "title": f"Sky: Children of the Light {version_name} (vc {version_code})",
        },
    }


def command_probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    details, source, failures = fetch_play_details(config)
    manifest = build_probe_manifest(config, details, source)
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
        "source": source,
        "degraded": bool(failures),
        "failures": "; ".join(failures),
    }
    write_github_outputs(outputs)
    summary = [
        f"**Probe:** {manifest['app']['versionName']} (vc {manifest['app']['versionCode']}) via `{source}`",
        f"**Decision:** {'release' if should_release else 'skip'} ({reason})",
    ]
    if failures:
        summary.append("**Fallback used:** " + "; ".join(failures))
    append_step_summary("\n\n".join(summary))
    print(json.dumps(outputs, ensure_ascii=False))
    return 0


def command_download(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    probe = load_json(args.probe)
    expected_cert = config["app"].get("signingCertSha256")
    failures: list[str] = []
    for source in download_sources(config):
        reset_dir(args.output_dir)
        try:
            DOWNLOADERS[source](config, args.output_dir)
            files = package_files(args.output_dir)
            if not files:
                raise SourceError(f"{source} produced no package files")
            signer = verify_signatures(files, expected_cert)
        except SourceError as exc:
            failures.append(f"{source}: {exc}")
            warn(f"download via {source} failed: {exc}")
            continue

        meta = {
            "downloader": {"name": source, "version": TOOL_VERSIONS[source]()},
            "playDigestsChecked": CHECKS_PLAY_DIGESTS[source],
            "signingCertSha256": signer,
            "signingCertPinned": bool(expected_cert),
            "failedSources": failures,
            "probeVersionCode": probe["app"]["versionCode"],
        }
        write_json(args.meta, meta)
        write_github_outputs({"source": source, "degraded": bool(failures), "failures": "; ".join(failures)})
        lines = [f"**Download:** {len(files)} files via `{source}`, signer `{signer}`"]
        if not expected_cert:
            lines.append("Signing certificate is not pinned yet (app.signingCertSha256).")
        if failures:
            lines.append("**Fallback used:** " + "; ".join(failures))
        append_step_summary("\n\n".join(lines))
        print(f"Downloaded {len(files)} files via {source}; signer {signer}")
        return 0

    write_github_outputs({"degraded": True, "failures": "; ".join(failures)})
    raise RuntimeError("every download source failed: " + "; ".join(failures))


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
    verification = manifest.get("verification") or {}
    downloader_name = (download.get("downloader") or {}).get("name", "gplaydl")
    signer = verification.get("signingCertSha256")
    verification_lines = []
    if verification.get("playDigestsCheckedByDownloader"):
        verification_lines.append(f"`{downloader_name}` 已按 Google Play 交付清单中的 SHA-256 校验每个文件。")
    if signer:
        verification_lines.append("所有 APK 均通过 `apksigner` 校验，且由同一个开发者证书签名。")
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
            *verification_lines,
            "本 Release 另附 `SHA256SUMS.txt`、`release-manifest.json` 和 `download-index.json`。",
            "",
            f"- Package: `{app['packageName']}`",
            f"- Google Play: {app['playUrl']}",
            f"- Downloader: `{downloader_name}`",
            *([f"- 签名证书 SHA-256: `{signer}`"] if signer else []),
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
    meta = load_json(args.download_meta) if getattr(args, "download_meta", None) else {}
    downloader = meta.get("downloader") or {"name": "gplaydl", "version": gplaydl_version()}
    manifest["download"] = {**manifest["download"], "downloader": downloader}
    manifest["verification"] = {
        "algorithm": "sha256",
        "checksumsAsset": "SHA256SUMS.txt",
        "playDigestsCheckedByDownloader": bool(
            meta.get("playDigestsChecked", CHECKS_PLAY_DIGESTS.get(downloader["name"], False))
        ),
        "signingCertSha256": meta.get("signingCertSha256"),
        "signingCertPinned": bool(meta.get("signingCertPinned", False)),
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

    download = subparsers.add_parser("download", help="download from Google Play, falling back between sources")
    download.add_argument("--config", type=Path, default=Path("mirror-config.json"))
    download.add_argument("--probe", type=Path, required=True)
    download.add_argument("--output-dir", type=Path, required=True)
    download.add_argument("--meta", type=Path, required=True)
    download.set_defaults(func=command_download)

    finalize = subparsers.add_parser("finalize", help="verify downloads and build release metadata")
    finalize.add_argument("--config", type=Path, default=Path("mirror-config.json"))
    finalize.add_argument("--probe", type=Path, required=True)
    finalize.add_argument("--input-dir", type=Path, required=True)
    finalize.add_argument("--output-dir", type=Path, required=True)
    finalize.add_argument("--repository", required=True)
    finalize.add_argument("--download-meta", type=Path)
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
