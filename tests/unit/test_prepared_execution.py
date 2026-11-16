"""Synthetic protocol/source proof. Real Linux socket/proc custody is a separate gate."""
import base64
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import prepared_execution as execution
import prepared_channels as channels

NONCE = "a" * 64
NEXT_NONCE = "b" * 64
PRODUCER_BYTES = b"isolated synthetic producer identity, never a release approval"


def approval():
    return {"schema_version": 1, "artifact_kind": "full-producer", "source_commit": "a" * 40,
            "upstream_commit": execution.UPSTREAM, "producer_sha256": hashlib.sha256(PRODUCER_BYTES).hexdigest(),
            "core_sha256": execution.CORE, "release_manifest_sha256": "2" * 64}


def snapshot(nonce=NONCE):
    fixture = json.loads((ROOT / "tests/fixtures/prepared-execution-go-json-vectors.json").read_text(encoding="utf-8"))
    value = execution._decode(base64.b64decode(fixture["native_snapshot_base64"]))
    value["challenge"] = nonce
    return value


class PreparedExecution(unittest.TestCase):
    def test_actual_go_canonical_vectors_match_or_refuse_explicitly(self):
        data = json.loads((ROOT / "tests/fixtures/prepared-execution-go-json-vectors.json").read_text())
        self.assertEqual(data["producer_upstream"], execution.UPSTREAM)
        for vector in data["vectors"]:
            with self.subTest(vector=vector["name"]):
                raw = base64.b64decode(vector["input_base64"])
                if vector["consumer_supported"]:
                    parsed = execution._decode(raw)
                    self.assertEqual(channels.digest(parsed), vector["sha256"])
                    self.assertEqual(json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(), base64.b64decode(vector["canonical_base64"]))
                else:
                    with self.assertRaises(execution.ExecutionUnavailable):
                        execution._decode(raw)

    def test_json_bounds_nonfinite_and_ambiguous_input_refuse(self):
        for raw in (b"x", b'{"n":NaN}', b'{"n":Infinity}', b'{} {}', b"[" * 1100 + b"]" * 1100,
                    b" " * (execution.LIMIT + 1)):
            with self.subTest(size=len(raw)):
                with self.assertRaises(execution.ExecutionUnavailable):
                    execution._decode(raw)

    def test_current_complete_snapshot_projects_existing_runtime_view(self):
        value = snapshot()
        view, config = execution._snapshot(value, NONCE, 10, execution._approval(approval()))
        self.assertEqual(view["engine_instance"]["pid"], 20)
        self.assertEqual(view["mutation_epoch"], "0")
        self.assertEqual(view["selector_transitions"], [])
        self.assertEqual(channels.qualified_effective_config(config, view), value["effective_config"])
        self.assertNotIn("prepared_eu_paths_v1", view)
        self.assertNotIn("public_profile", view)

    def test_actual_go_wire_epoch_is_projected_without_accepting_a_compiler_alias(self):
        value = snapshot()
        self.assertEqual(value["epoch"], "0")
        self.assertNotIn("mutation_epoch", value)
        view, _ = execution._snapshot(value, NONCE, 10, approval())
        self.assertEqual(view["mutation_epoch"], value["epoch"])
        value["mutation_epoch"] = value.pop("epoch")
        with self.assertRaisesRegex(execution.ExecutionUnavailable, "^SNAPSHOT_SHAPE$"):
            execution._snapshot(value, NONCE, 10, approval())

    def test_snapshot_header_context_and_hash_fail_closed(self):
        changes = [{"schema_version": 2}, {"schema_version": True}, {"challenge": NEXT_NONCE},
                   {"qualified": False}, {"qualified": 1}, {"reason": "unknown"},
                   {"epoch": True}, {"epoch": "01"}, {"epoch": "18446744073709551616"},
                   {"effective_config_sha256": "0" * 64}, {"execution_sha256": "0" * 64},
                   {"effective_config": {"private": "DO_NOT_LEAK"}}, {"balancer_overrides": {"b": "target"}},
                   {"balancer_overrides": []}, {"unexpected": "DO_NOT_LEAK"}]
        for change in changes:
            with self.subTest(keys=list(change)):
                with self.assertRaises((execution.ExecutionUnavailable, ValueError)) as result:
                    execution._snapshot({**snapshot(), **change}, NONCE, 10, approval())
                self.assertNotIn("DO_NOT_LEAK", str(result.exception))

    def test_actual_go_unqualified_frame_has_no_config_and_cannot_reuse_a_view(self):
        fixture = json.loads((ROOT / "tests/fixtures/prepared-execution-go-json-vectors.json").read_text(encoding="utf-8"))
        value = execution._decode(base64.b64decode(fixture["native_unqualified_snapshot_base64"]))
        self.assertIs(value["qualified"], False)
        self.assertEqual(value["epoch"], "1")
        for field in ("effective_config", "effective_config_sha256", "execution_sha256"):
            self.assertNotIn(field, value)
        with self.assertRaisesRegex(execution.ExecutionUnavailable, "^EXECUTION_UNQUALIFIED$"):
            execution._snapshot(value, NONCE, 10, approval())

    def test_process_and_release_assertions_are_strict(self):
        changes = [{"producer_pid": True}, {"producer_pid": 11}, {"producer_sha256": "0" * 64},
                   {"core_sha256": "0" * 64}, {"producer_upstream": "b" * 40},
                   {"pid": True}, {"pid": -1}, {"generation": "not-a-generation"},
                   {"boot_id": "not-a-boot"}, {"start_ticks": "01"}, {"producer_start_ticks": "-1"},
                   {"consumed_sha256": "bad"}, {"extra": 1}]
        for change in changes:
            with self.subTest(keys=list(change)):
                value = snapshot()
                value["identity"].update(change)
                with self.assertRaises((execution.ExecutionUnavailable, ValueError)):
                    execution._snapshot(value, NONCE, 10, approval())

    def test_synthetic_artifact_or_incomplete_approval_is_not_a_release(self):
        for change in ({"artifact_kind": "synthetic-fixture"}, {"artifact_kind": "test-elf"},
                       {"schema_version": True}, {"core_sha256": "4" * 64},
                       {"release_manifest_sha256": None}, {"source_commit": "main"}, {"extra": True}):
            with self.assertRaises(execution.ExecutionUnavailable):
                execution._approval({**approval(), **change})

    def test_selector_receipt_must_end_at_current_generation_epoch_and_hash(self):
        value = snapshot()
        value["epoch"] = "1"
        receipt = {"generation": value["identity"]["generation"], "from_epoch": "0", "to_epoch": "1",
                   "before_sha256": "4" * 64, "after_sha256": value["effective_config_sha256"],
                   "normalized_sha256": "5" * 64, "rule_tag": "wm-rule-fixture"}
        value["selector_transition"] = receipt
        self.assertEqual(execution._snapshot(value, NONCE, 10, approval())[0]["selector_transitions"], [receipt])
        for change in ({"generation": "00000000-0000-4000-8000-000000000003"}, {"to_epoch": "2"},
                       {"after_sha256": "6" * 64}, {"from_epoch": "01"}, {"rule_tag": "api"}):
            with self.assertRaises((execution.ExecutionUnavailable, ValueError)):
                execution._snapshot({**value, "selector_transition": {**receipt, **change}}, NONCE, 10, approval())

    def test_private_socket_and_file_modes_do_not_accept_symlinks_or_other_uid(self):
        execution._private(SimpleNamespace(st_uid=0, st_mode=stat.S_IFSOCK | 0o600), stat.S_ISSOCK, 0o600)
        for uid, mode in ((1000, stat.S_IFSOCK | 0o600), (0, stat.S_IFSOCK | 0o604), (0, stat.S_IFLNK | 0o600)):
            with self.assertRaises(execution.ExecutionUnavailable):
                execution._private(SimpleNamespace(st_uid=uid, st_mode=mode), stat.S_ISSOCK, 0o600)

    def test_proc_stat_parsing_pins_start_instance_and_parent(self):
        fields = ["S", "10"] + ["0"] * 17 + ["200"]
        raw = "20 (name ) with spaces) " + " ".join(fields)
        with mock.patch.object(Path, "read_text", side_effect=[raw, "Uid:\t0\t0\t0\t0\n"]):
            self.assertEqual(execution._process(20), {"pid": 20, "parent_pid": 10, "start_ticks": "200"})
        for raw in ("20 invalid", "21 (other) " + " ".join(fields), "20 (zombie) " + " ".join(["Z"] + fields[1:])):
            with mock.patch.object(Path, "read_text", return_value=raw):
                with self.assertRaises(execution.ExecutionUnavailable):
                    execution._process(20)
        for status in ("", "Uid:\t1000\t1000\t1000\t1000\n", "Uid:\t0\t0\n"):
            with mock.patch.object(Path, "read_text", side_effect=["20 (root) " + " ".join(fields), status]):
                with self.assertRaises(execution.ExecutionUnavailable):
                    execution._process(20)

    def frame_fixture(self, raw=None, uid=0, pid=10, expected=10, changed=False, expired=False):
        raw = json.dumps(snapshot(), separators=(",", ":"), ensure_ascii=False).encode() + b"\n" if raw is None else raw
        directory = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700, st_dev=1, st_ino=1)
        endpoint = SimpleNamespace(st_uid=0, st_mode=stat.S_IFSOCK | 0o600, st_dev=1, st_ino=2)
        other = SimpleNamespace(st_uid=0, st_mode=stat.S_IFSOCK | 0o600, st_dev=1, st_ino=3)
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.getsockopt.return_value = struct.pack("3i", pid, uid, 0)
        connection.recv.side_effect = [raw[:17], raw[17:], b""]
        folder, path = mock.Mock(), mock.Mock()
        folder.lstat.return_value = directory
        path.lstat.side_effect = [endpoint, endpoint, other if changed else endpoint, endpoint]
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(execution, "DIRECTORY", folder))
            patches.enter_context(mock.patch.object(execution, "SNAPSHOT", path))
            patches.enter_context(mock.patch.object(execution.socket, "socket", return_value=connection))
            patches.enter_context(mock.patch.object(execution.socket, "SO_PEERCRED", 17, create=True))
            patches.enter_context(mock.patch.object(execution.socket, "AF_UNIX", 1, create=True))
            patches.enter_context(mock.patch.object(execution.secrets, "token_hex", return_value=NONCE))
            patches.enter_context(mock.patch.object(execution.time, "monotonic", side_effect=[0, 4] if expired else None, return_value=0))
            try:
                result = execution._frame(expected)
            except execution.ExecutionUnavailable as error:
                result = error
        return result, connection

    def test_stream_frame_uses_fresh_challenge_and_reads_exact_single_reply_to_eof(self):
        result, connection = self.frame_fixture()
        self.assertEqual(result, (snapshot(), NONCE, 10))
        sent = connection.sendall.call_args.args[0]
        self.assertEqual(json.loads(sent), {"schema_version": 3, "challenge": NONCE})
        self.assertEqual(connection.recv.call_count, 3)

    def test_untrusted_uid_or_changed_peer_is_refused_before_request(self):
        for kwargs in ({"uid": 1000}, {"pid": 0}, {"pid": 11}):
            result, connection = self.frame_fixture(**kwargs)
            self.assertIsInstance(result, execution.ExecutionUnavailable)
            connection.sendall.assert_not_called()

    def test_multiple_truncated_oversize_or_replaced_socket_frames_refuse(self):
        for raw in (b"{}", b"{}\n{}\n", b"{}\nextra", b" " * (execution.LIMIT + 1)):
            result, _ = self.frame_fixture(raw=raw)
            self.assertIsInstance(result, execution.ExecutionUnavailable)
        result, _ = self.frame_fixture(changed=True)
        self.assertEqual(str(result), "SNAPSHOT_FRAME")

    def test_snapshot_deadline_does_not_extend_with_stream_chunks(self):
        result, connection = self.frame_fixture(expired=True)
        self.assertEqual(str(result), "SNAPSHOT_TIMEOUT")
        connection.recv.assert_not_called()

    def collect_fixture(self, second=None, frame_error=None, bad_pin=False):
        stream = tempfile.TemporaryFile()
        stream.write(PRODUCER_BYTES)
        stream.seek(0)
        first = snapshot()
        second = snapshot(NEXT_NONCE) if second is None else second
        pins = approval()
        if bad_pin:
            pins["producer_sha256"] = "0" * 64
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(execution.sys, "platform", "linux"))
            patches.enter_context(mock.patch.object(execution, "_load_approval", return_value=pins))
            patches.enter_context(mock.patch.object(execution, "_connection", return_value=contextlib.nullcontext((None, 10, None))))
            patches.enter_context(mock.patch.object(execution, "_process", return_value={"pid": 10, "start_ticks": "100", "parent_pid": 1}))
            patches.enter_context(mock.patch.object(execution, "_open", side_effect=lambda path, stack: stack.enter_context(stream)))
            frames = patches.enter_context(mock.patch.object(execution, "_frame", side_effect=frame_error or [(first, NONCE, 10), (second, second["challenge"], 10)]))
            observed = patches.enter_context(mock.patch.object(execution, "_observe", return_value="observed"))
            rechecked = patches.enter_context(mock.patch.object(execution, "_recheck"))
            try:
                result = execution.collect()
            except execution.ExecutionUnavailable as error:
                result = error
        self.assertTrue(stream.closed)
        return result, frames, observed, rechecked

    def test_two_fresh_frames_bracket_independent_identity_inspection(self):
        result, frames, observed, rechecked = self.collect_fixture()
        self.assertIsInstance(result, tuple)
        self.assertEqual(frames.call_count, 2)
        observed.assert_called_once()
        rechecked.assert_called_once_with("observed")

    def test_changed_epoch_even_equal_json_or_replayed_nonce_cannot_be_current(self):
        for second in ({**snapshot(NEXT_NONCE), "epoch": "2"}, snapshot(NONCE)):
            result, _, _, rechecked = self.collect_fixture(second=second)
            self.assertIsInstance(result, execution.ExecutionUnavailable)
            self.assertEqual(str(result), "EXECUTION_CHANGED_DURING_COLLECTION")
            rechecked.assert_not_called()

    def test_unapproved_executable_is_refused_before_any_private_snapshot_request(self):
        result, frames, observed, _ = self.collect_fixture(bad_pin=True)
        self.assertEqual(str(result), "EXECUTABLE_PIN")
        frames.assert_not_called()
        observed.assert_not_called()

    def test_failure_releases_open_evidence_and_uses_fixed_error_only(self):
        result, _, observed, _ = self.collect_fixture(frame_error=OSError("DO_NOT_LEAK private payload"))
        self.assertEqual(str(result), "EXECUTION_NOT_VERIFIED")
        observed.assert_not_called()

    def test_default_missing_release_approval_never_connects(self):
        with mock.patch.object(execution.sys, "platform", "linux"), mock.patch.object(execution, "_load_approval", side_effect=FileNotFoundError("private")), mock.patch.object(execution, "_connection") as connection:
            with self.assertRaisesRegex(execution.ExecutionUnavailable, "^EXECUTION_NOT_VERIFIED$"):
                execution.collect()
            connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
