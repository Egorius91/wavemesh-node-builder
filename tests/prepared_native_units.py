"""Mandatory synthetic prepared units; real Linux/root execution is required.

This entrypoint does not qualify a real Producer, Node, VPN or live service.
CI invokes it in a fresh network namespace on its disposable Linux runner.
"""
import importlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "test_prepared_owning_caller": 32,
    "test_prepared_owning_transaction": 6,
    "test_prepared_transaction": 6,
    "test_agent_prepared_origin": 5,
}
ROOT_COMPLETION_METHODS = frozenset({
    "PreparedCompletionNativeOwner.test_native_existing_fd9_is_held_until_caller_closes_it",
    "PreparedCompletionNativeOwner.test_native_foreign_inode_and_weak_lock_are_refused",
})


def admission_error(platform, euid, bash, version, interfaces):
    """Pure admission decision; passing synthetic inputs is not native proof."""
    if platform != "linux" or euid != 0 or not bash or version < (3, 12):
        return "PREPARED_UNITS_REQUIRE_LINUX_ROOT_BASH_PY312"
    if set(interfaces) != {"lo"}:
        return "PREPARED_UNITS_REQUIRE_PRIVATE_LOOPBACK_ONLY_NAMESPACE"
    return None


def require_native():
    # Do not open Linux paths or import fixture/product code before this guard.
    if sys.platform != "linux" or getattr(os, "geteuid", lambda: -1)() != 0:
        raise RuntimeError("PREPARED_UNITS_REQUIRE_LINUX_ROOT_BASH_PY312")
    interfaces = [line.split(":", 1)[0].strip()
                  for line in Path("/proc/net/dev").read_text().splitlines() if ":" in line]
    error = admission_error(sys.platform, os.geteuid(), shutil.which("bash"),
                            sys.version_info[:2], interfaces)
    if error:
        raise RuntimeError(error)


def load_suite(name, expected):
    # Preserve both package imports and existing unqualified fixture imports.
    sys.path[:0] = [str(ROOT), str(ROOT / "tests/unit")]
    for package, relative in (("tests", "tests/__init__.py"),
                              ("tests.unit", "tests/unit/__init__.py")):
        module = importlib.import_module(package)
        if Path(module.__file__).resolve() != (ROOT / relative).resolve():
            raise RuntimeError("PREPARED_UNITS_FOREIGN_TEST_PACKAGE")
    module = importlib.import_module("tests.unit." + name)
    if Path(module.__file__).resolve() != (ROOT / "tests/unit" / (name + ".py")).resolve():
        raise RuntimeError("PREPARED_UNITS_FOREIGN_TEST_MODULE")
    suite = unittest.TestLoader().loadTestsFromModule(module)
    if suite.countTestCases() != expected:
        raise RuntimeError("PREPARED_UNITS_COVERAGE_CHANGED_" + name)
    return suite


def cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from cases(case)
        else:
            yield case


def completion_roles():
    module = "tests.unit.test_prepared_completion."
    ordinary, native = [], []
    suite = load_suite("test_prepared_completion", 15)
    for case in cases(suite):
        case_id = case.id()
        if not case_id.startswith(module):
            raise RuntimeError("PREPARED_COMPLETION_FOREIGN_CASE")
        (native if case_id[len(module):] in ROOT_COMPLETION_METHODS else ordinary).append(case)
    selected = {case.id()[len(module):] for case in native}
    if selected != ROOT_COMPLETION_METHODS or len(native) != 2 or len(ordinary) != 13:
        raise RuntimeError("PREPARED_COMPLETION_ROLE_COVERAGE_CHANGED")
    return unittest.TestSuite(ordinary), unittest.TestSuite(native)


def native_suites():
    # The accepted original49 stays explicit; the existing6 are additive.
    for name, expected in SUITES.items():
        yield name, load_suite(name, expected), expected
    yield "test_prepared_panel_peer_native", load_suite("test_prepared_panel_peer_native", 4), 4
    _, native_completion = completion_roles()
    yield "test_prepared_completion.root", native_completion, 2


def ordinary_completion():
    ordinary, _ = completion_roles()
    result = unittest.TextTestRunner(verbosity=2).run(ordinary)
    return int(not completed_without_skips(result, 13))


def identity(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("PREPARED_UNITS_TEMP_UNSAFE")
    return info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def completed_without_skips(result, expected):
    return result.testsRun == expected and result.wasSuccessful() and not result.skipped


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--ordinary-completion"]:
        return ordinary_completion()
    if arguments:
        raise RuntimeError("PREPARED_UNITS_UNKNOWN_ROLE")
    require_native()
    previous_umask = os.umask(0o077)
    previous_environment = os.environ.copy()
    previous_tempdir = tempfile.tempdir
    outer = path = pin = None
    primary = cleanup_failed = 1
    try:
        outer = tempfile.TemporaryDirectory(prefix="wm-prepared-unit-ci-", dir="/tmp", delete=False)
        path = Path(outer.name)
        pin = identity(path)
        os.environ.clear()
        os.environ.update({"HOME": str(path), "TMPDIR": str(path), "LANG": "C.UTF-8",
            "PATH": str(Path(sys.executable).parent) + ":/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1"})
        tempfile.tempdir = str(path)
        primary = 0
        for name, suite, expected in native_suites():
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not completed_without_skips(result, expected):
                primary = 1
        # All fixtures must have performed their own cleanup before outer removal.
        if identity(path) != pin or any(path.iterdir()):
            raise RuntimeError("PREPARED_UNITS_FIXTURE_RESIDUE")
    finally:
        try:
            if outer is not None and pin is not None and identity(path) == pin:
                outer.cleanup()
                if os.path.lexists(path):
                    raise RuntimeError("PREPARED_UNITS_TEMP_REMAINS")
                cleanup_failed = 0
        finally:
            tempfile.tempdir = previous_tempdir
            os.environ.clear()
            os.environ.update(previous_environment)
            os.umask(previous_umask)
    return primary or cleanup_failed


if __name__ == "__main__":
    raise SystemExit(main())
