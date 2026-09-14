#!/usr/bin/env python3
"""Apply the reviewed source-only backend patch to an exact clean upstream checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    source = Path(sys.argv[1]).resolve(strict=True)
    bundle = Path(__file__).resolve().parents[2] / "third_party" / "3x-ui"
    manifest = json.loads((bundle / "disabled-create.json").read_text(encoding="utf-8"))
    patch = bundle / "disabled-create.patch"
    def git(*args):
        return subprocess.run(["git", "-c", f"safe.directory={source.as_posix()}", "-C", str(source), *args],
                              check=True, capture_output=True).stdout
    if git("rev-parse", "HEAD").decode().strip() != manifest["upstream_commit"]:
        raise RuntimeError("upstream commit mismatch")
    if (git("status", "--porcelain", "--untracked-files=all").strip()
            or git("ls-files", "--others").strip()):
        raise RuntimeError("upstream checkout must be clean")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise RuntimeError("patch checksum mismatch")
    git("apply", "--check", str(patch))
    git("apply", str(patch))
    git("diff", "--check")
    print("PINNED_BACKEND_PATCH=APPLIED; RUNTIME_MUTATION=NONE")


if __name__ == "__main__":
    main()
