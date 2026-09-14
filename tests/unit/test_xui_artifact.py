#!/usr/bin/env python3
"""Candidate integrity tests, including tampering with a rehashed manifest."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("xui_artifact", ROOT / "scripts/ci/xui_artifact.py")
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)
HEAD = "a" * 40


class ArtifactTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_bundle = artifact.BUNDLE
        artifact.BUNDLE = self.root / "spec"
        artifact.BUNDLE.mkdir()
        self.addCleanup(setattr, artifact, "BUNDLE", self.old_bundle)
        self.release = self.root / "release.tar.gz"
        self.make_tar(self.release, {artifact.PANEL: b"official", "x-ui/bin/xray": b"pinned-runtime"})
        self.spec = {"sha256": artifact.digest(self.release.read_bytes()),
                     "members": artifact.inventory(self.release)}
        self.save(artifact.BUNDLE / "runtime-release.json", self.spec)
        self.patch = {"upstream_commit": "b" * 40, "patch_sha256": artifact.digest(b"patch")}
        self.save(artifact.BUNDLE / "disabled-create.json", self.patch)
        self.source = self.root / "upstream"
        frontend = self.source / "internal/web/dist"
        frontend.mkdir(parents=True)
        (frontend / "index.html").write_text("<script src='app.js'></script>")
        (frontend / "app.js").write_text("x" * 2048)
        self.binary = self.root / "panel"
        self.binary.write_bytes(b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00" + b"compiled")
        self.output = self.root / "candidate"
        self.output.mkdir()
        self.make_tar(self.output / "source.tar.gz", {
            "upstream/LICENSE": b"license", "upstream/frontend/package-lock.json": b"{}",
            "upstream/go.sum": b"sum", "builder/scripts/ci/xui_artifact.py": b"script",
            "builder/third_party/3x-ui/disabled-create.patch": b"patch",
            "WAVEMESH-MODIFICATIONS.txt": b"notice",
        })
        self.tools = self.root / "tools"
        self.tools.write_text("test fixture")

    def save(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def make_tar(self, path, files):
        with tarfile.open(path, "w:gz") as stream:
            for name, value in files.items():
                artifact.add_bytes(stream, name, value, 0o755 if name == artifact.PANEL else 0o644)

    def package(self):
        artifact.package(self.source, self.release, self.binary, self.output, HEAD, self.tools)

    def test_full_inventory_preserves_runtime_and_replaces_panel(self):
        self.package()
        actual = artifact.inventory(self.output / "x-ui-linux-amd64-wavemesh.tar.gz")
        self.assertEqual(actual["x-ui/bin/xray"], self.spec["members"]["x-ui/bin/xray"])
        self.assertNotEqual(actual[artifact.PANEL], self.spec["members"][artifact.PANEL])
        artifact.verify(self.output, HEAD)

    def test_changed_dependency_rejected_even_after_manifest_rehash(self):
        self.package()
        archive = self.output / "x-ui-linux-amd64-wavemesh.tar.gz"
        self.make_tar(archive, {artifact.PANEL: self.binary.read_bytes(), "x-ui/bin/xray": b"changed"})
        manifest = artifact.read_json(self.output / "manifest.json")
        manifest.update(archive_sha256=artifact.digest(archive.read_bytes()), members=artifact.inventory(archive))
        self.save(self.output / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "dependency changed"):
            artifact.verify(self.output, HEAD)

    def test_extra_file_rejected_even_after_manifest_rehash(self):
        self.package()
        archive = self.output / "x-ui-linux-amd64-wavemesh.tar.gz"
        self.make_tar(archive, {artifact.PANEL: self.binary.read_bytes(), "x-ui/bin/xray": b"pinned-runtime", "extra": b"x"})
        manifest = artifact.read_json(self.output / "manifest.json")
        manifest.update(archive_sha256=artifact.digest(archive.read_bytes()), members=artifact.inventory(archive))
        self.save(self.output / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            artifact.verify(self.output, HEAD)

    def test_wrong_head_and_changed_source_rejected(self):
        self.package()
        with self.assertRaisesRegex(ValueError, "provenance mismatch"):
            artifact.verify(self.output, "c" * 40)
        (self.output / "source.tar.gz").write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            artifact.verify(self.output, HEAD)

    def test_release_tampering_and_missing_frontend_rejected(self):
        (self.source / "internal/web/dist/index.html").unlink()
        with self.assertRaisesRegex(ValueError, "frontend missing"):
            self.package()
        self.release.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "release digest"):
            self.package()

    def test_unsafe_archive_members(self):
        for name in ("../escape", "/absolute", "a/../b", "a/./b", "a\\b"):
            with self.subTest(name=name):
                self.make_tar(self.release, {name: b"x"})
                with self.assertRaisesRegex(ValueError, "unsafe archive"):
                    artifact.inventory(self.release)
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                with tarfile.open(self.release, "w:gz") as stream:
                    member = tarfile.TarInfo("link")
                    member.type, member.linkname = kind, "target"
                    stream.addfile(member)
                with self.assertRaisesRegex(ValueError, "unsafe archive"):
                    artifact.inventory(self.release)

    def test_duplicate_and_setuid_members_rejected(self):
        with tarfile.open(self.release, "w:gz") as stream:
            artifact.add_bytes(stream, "same", b"a")
            artifact.add_bytes(stream, "same", b"b")
        with self.assertRaisesRegex(ValueError, "unsafe archive"):
            artifact.inventory(self.release)
        with tarfile.open(self.release, "w:gz") as stream:
            artifact.add_bytes(stream, "setuid", b"x", 0o4755)
        with self.assertRaisesRegex(ValueError, "unsafe archive"):
            artifact.inventory(self.release)


if __name__ == "__main__":
    unittest.main()
