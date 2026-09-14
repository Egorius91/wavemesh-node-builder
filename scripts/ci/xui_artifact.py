#!/usr/bin/env python3
"""Package and independently inspect a full, non-promoted panel candidate.

Never extracts release archives or installs/executes their contents.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "third_party/3x-ui"
PANEL = "x-ui/x-ui"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def git(source, *args):
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={source.as_posix()}", "-C", str(source), *args]
    )


def revision(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("expected a full source commit")
    return value


def inventory(archive):
    """Reject unsafe/duplicate/special members before accepting an inventory."""
    result = {}
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or str(path) != member.name or ".." in path.parts
                    or "\\" in member.name or not path.parts
                    or member.name in result or not (member.isfile() or member.isdir())
                    or member.mode & ~0o777):
                raise ValueError("unsafe archive member")
            row = {"kind": "directory" if member.isdir() else "file",
                   "mode": member.mode, "size": member.size}
            if member.isfile():
                h = hashlib.sha256()
                with stream.extractfile(member) as data:
                    for block in iter(lambda: data.read(1024 * 1024), b""):
                        h.update(block)
                row["sha256"] = h.hexdigest()
            result[member.name] = row
    return result


def add_bytes(stream, name, data, mode=0o644):
    member = tarfile.TarInfo(name)
    member.size, member.mode = len(data), mode
    member.uid = member.gid = member.mtime = 0
    stream.addfile(member, io.BytesIO(data))


def source_bundle(source, output, head):
    """Run after applying the reviewed patch, before npm/build generated files."""
    head = revision(head)
    patch = read_json(BUNDLE / "disabled-create.json")
    if git(ROOT, "rev-parse", "HEAD").decode().strip() != head:
        raise ValueError("builder source mismatch")
    git(ROOT, "diff", "--exit-code", "HEAD")
    if git(source, "rev-parse", "HEAD").decode().strip() != patch["upstream_commit"]:
        raise ValueError("upstream source mismatch")
    label = f"3.4.2-wavemesh.{head[:12]}"
    (source / "internal/config/version").write_text(label + "\n", encoding="utf-8")
    output.mkdir(exist_ok=False)
    with tarfile.open(output / "source.tar.gz", "w:gz") as stream:
        for prefix, root in (("upstream", source), ("builder", ROOT)):
            names = set(git(root, "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard").decode().split("\0"))
            # Builder untracked CI download/build directories are never sources.
            if root == ROOT:
                names = set(git(root, "ls-files", "-z").decode().split("\0"))
            for name in sorted(names - {""}):
                path = root / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("source must consist of regular files")
                add_bytes(stream, f"{prefix}/{name}", path.read_bytes(),
                          0o755 if path.stat().st_mode & 0o111 else 0o644)
        notice = (f"WaveMesh-modified 3X-UI candidate {label}.\n"
                  f"Builder source: {head}\nUpstream source: {patch['upstream_commit']}\n"
                  "Changes: disabled-create.patch and internal/config/version build label.\n"
                  "See upstream/LICENSE and builder/third_party/3x-ui/LICENSE (GPL-3.0).\n"
                  "Build instructions: builder/docs/panel-artifact.md. Not promoted.\n")
        add_bytes(stream, "WAVEMESH-MODIFICATIONS.txt", notice.encode())
    print(f"SOURCE_BUNDLE=PASS; VERSION={label}")


def package(source, release, binary, output, head, toolchain):
    head = revision(head)
    spec = read_json(BUNDLE / "runtime-release.json")
    if digest(release.read_bytes()) != spec["sha256"]:
        raise ValueError("release digest mismatch")
    original = inventory(release)
    if original != spec["members"]:
        raise ValueError("release inventory mismatch")
    data = binary.read_bytes()
    # ELF64, little endian, x86-64. Static linkage is separately checked in CI.
    if data[:6] != b"\x7fELF\x02\x01" or data[18:20] != b"\x3e\x00":
        raise ValueError("not a Linux amd64 panel")
    frontend = source / "internal/web/dist"
    if not (frontend / "index.html").is_file():
        raise ValueError("real frontend missing")
    files = sorted(p for p in frontend.rglob("*") if p.is_file())
    if not any(p.suffix == ".js" and p.stat().st_size > 1024 for p in files):
        raise ValueError("frontend assets missing")
    frontend_hashes = {p.relative_to(frontend).as_posix(): digest(p.read_bytes()) for p in files}
    candidate = output / "x-ui-linux-amd64-wavemesh.tar.gz"
    with tarfile.open(release, "r:gz") as src, tarfile.open(candidate, "w:gz") as dst:
        for member in src:
            if member.name == PANEL:
                add_bytes(dst, PANEL, data, 0o755)
            elif member.isfile():
                add_bytes(dst, member.name, src.extractfile(member).read(), member.mode)
            else:
                clean = tarfile.TarInfo(member.name)
                clean.type, clean.mode = tarfile.DIRTYPE, member.mode
                dst.addfile(clean)
    manifest = {
        "schema": 1, "status": "CI_CANDIDATE_NOT_DEPLOYED", "platform": "linux-amd64",
        "builder_commit": head, "upstream": read_json(BUNDLE / "disabled-create.json"),
        "version": f"3.4.2-wavemesh.{head[:12]}", "runtime_release_sha256": spec["sha256"],
        "source_sha256": digest((output / "source.tar.gz").read_bytes()),
        "archive_sha256": digest(candidate.read_bytes()), "members": inventory(candidate),
        "frontend": frontend_hashes, "toolchain": toolchain.read_text(encoding="utf-8"),
        "workflow_run": os.environ.get("GITHUB_RUN_ID"),
        "workflow_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify(output, head)


def verify(output, head):
    head = revision(head)
    manifest = read_json(output / "manifest.json")
    spec = read_json(BUNDLE / "runtime-release.json")
    if (manifest["schema"] != 1 or manifest["builder_commit"] != head
            or manifest["upstream"] != read_json(BUNDLE / "disabled-create.json")
            or manifest["version"] != f"3.4.2-wavemesh.{head[:12]}"
            or manifest["runtime_release_sha256"] != spec["sha256"]):
        raise ValueError("candidate provenance mismatch")
    archive = output / "x-ui-linux-amd64-wavemesh.tar.gz"
    if (digest(archive.read_bytes()) != manifest["archive_sha256"]
            or digest((output / "source.tar.gz").read_bytes()) != manifest["source_sha256"]):
        raise ValueError("candidate checksum mismatch")
    actual = inventory(archive)
    if actual != manifest["members"] or set(actual) != set(spec["members"]):
        raise ValueError("candidate inventory mismatch")
    for name, row in actual.items():
        if name != PANEL and row != spec["members"][name]:
            raise ValueError("runtime dependency changed")
    if actual[PANEL]["mode"] != 0o755 or actual[PANEL]["sha256"] == spec["members"][PANEL]["sha256"]:
        raise ValueError("panel was not replaced")
    sources = inventory(output / "source.tar.gz")
    for name in ("upstream/LICENSE", "upstream/frontend/package-lock.json",
                 "upstream/go.sum", "builder/scripts/ci/xui_artifact.py",
                 "builder/third_party/3x-ui/disabled-create.patch", "WAVEMESH-MODIFICATIONS.txt"):
        if name not in sources:
            raise ValueError("corresponding source missing")
    if sources["builder/third_party/3x-ui/disabled-create.patch"]["sha256"] != manifest["upstream"]["patch_sha256"]:
        raise ValueError("corresponding patch mismatch")
    print("CANDIDATE_INVENTORY=PASS; RUNTIME_DEPENDENCIES_UNCHANGED=PASS; DEPLOYMENT=NONE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["source", "package", "verify"])
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--toolchain", type=Path)
    args = parser.parse_args()
    if args.command == "source":
        source_bundle(args.source.resolve(), args.output.resolve(), args.head)
    elif args.command == "package":
        package(args.source, args.release, args.binary, args.output, args.head, args.toolchain)
    else:
        verify(args.output, args.head)


if __name__ == "__main__":
    main()
