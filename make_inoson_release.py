#!/usr/bin/env python3
"""Build the inoson PCM package and register it in the kicad-packages repo.

Fork releases use date-based versions ``YEAR.MONTH.N`` (N counts from 0
within a month), tagged ``inoson-YEAR.MONTH.N``, e.g. tag inoson-2026.11.0 ->
PCM version 2026.11.0. The upstream base (latest reachable ``v*`` tag) is
noted at the end of the package's description_full.

Release mode (default): HEAD must carry a fork tag equal to LAST_TAG in
InteractiveHtmlBom/version.py and tracked files must be clean. The new version
is added to packages/<identifier>/metadata.json in the kicad-packages checkout
and its build_repository.py is run.

Snapshot mode (--snapshot TAG): builds HEAD as TAG without these checks and
only writes the zip, unless --packages-repo is given explicitly.

Package metadata and icon come from the kicad-packages checkout
(default: ../kicad-packages). The zip goes to releases/<tag>/ and must be
uploaded as asset of the GitHub release <tag>.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASES_DIR = ROOT / "releases"
DEFAULT_PACKAGES_REPO = ROOT.parent / "kicad-packages"
IDENTIFIER = "de.inoson.InteractiveHtmlBom"
DOWNLOAD_URL = "https://github.com/inoson/InteractiveHtmlBom/releases/download/{tag}/{name}"
TAG_RE = re.compile(r"^inoson-(\d{4})\.(\d{1,2})\.(\d{1,6})$")
UPSTREAM_NOTE_RE = re.compile(r"\n*Based on upstream Interactive Html Bom \S+\.$")
DOWNLOAD_FIELDS = ("download_url", "download_sha256", "download_size", "install_size")
DEFAULT_VERSION_FIELDS = {
    "status": "stable",
    "kicad_version": "6.0",
    "kicad_version_max": "10",
    "platforms": ["linux", "macos", "windows"],
}


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, stderr=subprocess.STDOUT, text=True).strip()


def pcm_version_from_tag(tag: str) -> str:
    m = TAG_RE.match(tag)
    if not m:
        raise SystemExit(f"ERROR: tag '{tag}' does not match inoson-YEAR.MONTH.N")
    year, month, n = (int(g) for g in m.groups())
    if not 1 <= month <= 12:
        raise SystemExit(f"ERROR: month must be 1..12, got {month}")
    return f"{year}.{month}.{n}"


def upstream_base() -> str:
    return git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*",
               "--exclude", "*inoson*", "HEAD")


def with_upstream_note(description: str, base: str) -> str:
    note = f"Based on upstream Interactive Html Bom {base}."
    return UPSTREAM_NOTE_RE.sub("", description.rstrip()) + "\n" + note


def head_fork_tag() -> str:
    tags = [t for t in git("tag", "--points-at", "HEAD").splitlines()
            if TAG_RE.match(t)]
    if len(tags) != 1:
        raise SystemExit(
            f"ERROR: HEAD needs exactly one fork tag, found {tags or 'none'}")
    return tags[0]


def check_release_preconditions(tag: str) -> None:
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("ERROR: tracked files have uncommitted changes.")
    version_py = git("show", "HEAD:InteractiveHtmlBom/version.py")
    m = re.search(r"LAST_TAG\s*=\s*['\"]([^'\"]+)['\"]", version_py)
    if not m or m.group(1) != tag:
        found = m.group(1) if m else None
        raise SystemExit(f"ERROR: LAST_TAG ({found}) does not match tag ({tag})")


def zip_bytes(files: dict[str, bytes], date_time: tuple) -> bytes:
    """Deterministic zip with explicit directory entries."""
    dirs = set()
    for name in files:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]) + "/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(dirs | set(files)):
            info = zipfile.ZipInfo(name, date_time)
            if name.endswith("/"):
                info.external_attr = 0o40755 << 16 | 0x10
                zf.writestr(info, b"")
            else:
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, files[name])
    return buf.getvalue()


def plugin_files(ref: str, tag: str) -> dict[str, bytes]:
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", ref, "InteractiveHtmlBom"], cwd=ROOT)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name.split("/", 1)[1]
            if rel.endswith(".ini") or "__pycache__" in rel.split("/"):
                continue
            files["plugins/" + rel] = tar.extractfile(member).read()
    version_py = files["plugins/version.py"].decode("utf8")
    files["plugins/version.py"] = re.sub(
        r"LAST_TAG\s*=\s*['\"][^'\"]+['\"]", f"LAST_TAG = '{tag}'",
        version_py, count=1).encode("utf8")
    return files


def to_json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=4, ensure_ascii=False) + "\n").encode("utf8")


def new_version_entry(metadata: dict, version: str, status: str | None) -> dict:
    """Carry KiCad range/platforms over from the latest published version."""
    previous = metadata["versions"][0] if metadata["versions"] else DEFAULT_VERSION_FIELDS
    entry = {k: v for k, v in previous.items() if k not in DOWNLOAD_FIELDS}
    entry["version"] = version
    entry["status"] = status or "stable"
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", metavar="TAG",
                        help="build HEAD as TAG without tag/clean checks")
    parser.add_argument("--packages-repo", type=Path, default=None,
                        help=f"kicad-packages checkout (default: {DEFAULT_PACKAGES_REPO})")
    parser.add_argument("--status", default=None,
                        choices=["stable", "testing", "development"],
                        help="status of the new version (default: stable)")
    parser.add_argument("--replace", action="store_true",
                        help="replace an existing entry with the same version")
    args = parser.parse_args()

    if args.snapshot:
        tag = args.snapshot
    else:
        tag = head_fork_tag()
        check_release_preconditions(tag)
    version = pcm_version_from_tag(tag)
    print(f"Tag {tag} -> PCM version {version}")

    register = args.packages_repo is not None or not args.snapshot
    packages_repo: Path = args.packages_repo or DEFAULT_PACKAGES_REPO
    package_dir = packages_repo / "packages" / IDENTIFIER
    metadata_path = package_dir / "metadata.json"
    if not metadata_path.exists():
        raise SystemExit(f"ERROR: {metadata_path} not found")
    metadata = json.loads(metadata_path.read_text(encoding="utf8"))
    if register and any(v["version"] == version for v in metadata["versions"]) \
            and not args.replace:
        raise SystemExit(
            f"ERROR: version {version} already in {metadata_path} (use --replace)")

    base = upstream_base()
    print(f"Upstream base: {base}")
    metadata["description_full"] = with_upstream_note(metadata["description_full"], base)
    version_entry = new_version_entry(metadata, version, args.status)

    commit_time = int(git("log", "-1", "--format=%ct", "HEAD"))
    date_time = time.gmtime(commit_time)[:6]

    files = plugin_files("HEAD", tag)
    files["resources/icon.png"] = (package_dir / "icon.png").read_bytes()
    files["metadata.json"] = to_json_bytes(dict(metadata, versions=[version_entry]))
    install_size = sum(len(d) for d in files.values())

    package = zip_bytes(files, date_time)
    zip_name = f"InteractiveHtmlBom_{tag}.zip"
    out_dir = RELEASES_DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / zip_name).write_bytes(package)
    print(f"Package: {out_dir / zip_name} ({len(package)} bytes)")

    if not register:
        print("Snapshot: kicad-packages not touched (pass --packages-repo to update it)")
        return

    version_entry.update({
        "download_url": DOWNLOAD_URL.format(tag=tag, name=zip_name),
        "download_sha256": hashlib.sha256(package).hexdigest(),
        "download_size": len(package),
        "install_size": install_size,
    })
    metadata["versions"] = [version_entry] + [
        v for v in metadata["versions"] if v["version"] != version]
    metadata_path.write_bytes(to_json_bytes(metadata))
    print(f"Added version {version} to {metadata_path}")

    subprocess.check_call(
        [sys.executable, "build_repository.py"], cwd=packages_repo)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print("ERROR: command failed:\n", e.output, file=sys.stderr)
        sys.exit(2)
