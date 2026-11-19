"""Test-only full application composition. Never installed or run by product CLI.

Run ONLY after fresh native admission, inside a new offline guest and a private
mount/network namespace with loopback up. No mocks or alternate trust loaders.
Synthetic path health tests selector composition, not network/physical readiness.
"""
import argparse
from contextlib import closing
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # Absolute script entrypoint has no repository cwd on sys.path.
sys.path.insert(0, str(ROOT / "scripts/lib"))
import prepared_channels as channels
import prepared_completion as completion
import prepared_execution as execution
import prepared_intent as intent_writer
import prepared_journal as reader
import prepared_owner as owner
import runtime_state
import transaction_state as journal
from tests.unit.test_prepared_channels import fixture

PRODUCER = "d8af4a509d2888b52356f6ea950a3ed5151a1b9f45e182deef250c0b11d8768b"
PRODUCER_SOURCE = "fb44cd238cc086f8e706184b1745990ea700cfa6"
WORK = Path("/tmp/wm-pr4-full-composition-20261118")
INPUT = Path("/tmp/wm-pr4-composition-input-20261118")


PHASES = frozenset({"PRECHECK", "FIXTURE_PREPARATION", "MIGRATION", "DATABASE_FIXTURE",
                    "PRODUCER_START", "SNAPSHOT_WAIT", "INITIAL_COLLECT", "HEALTH_WAIT",
                    "SECOND_COLLECT", "INTENT_PREPARE", "OWNER_API", "FINAL_COLLECT", "POSTIMAGE_VERIFY"})
REASONS = frozenset({"FD9_OCCUPIED", "PRODUCER_EARLY_EXIT", "SNAPSHOT_WAIT_EXPIRED"})
COLLECTOR_CODES = frozenset({
    "UNSUPPORTED_JSON_INTEGER", "UNSUPPORTED_JSON_NUMBER", "AMBIGUOUS_JSON", "UNSUPPORTED_JSON_UNICODE",
    "UNSUPPORTED_JSON_VALUE", "SNAPSHOT_SIZE", "SNAPSHOT_SHAPE", "FULL_PRODUCER_APPROVAL_REQUIRED",
    "UNAPPROVED_ENGINE", "APPROVAL_SHAPE", "PRIVATE_CUSTODY_REQUIRED", "APPROVAL_CUSTODY_REQUIRED",
    "APPROVAL_SIZE", "SNAPSHOT_PEER", "SNAPSHOT_PEER_CHANGED", "SNAPSHOT_TIMEOUT", "SNAPSHOT_FRAME",
    "PROCESS_IDENTITY", "PROCESS_UID", "IMMUTABLE_INPUT_REQUIRED", "EXECUTABLE_PIN", "EXECUTABLE_OBJECT",
    "CONSUMED_SIZE", "CONSUMED_OBJECT", "CONSUMED_HASH", "PROCESS_CHANGED", "SNAPSHOT_CHALLENGE",
    "EXECUTION_UNQUALIFIED", "UNSUPPORTED_RUNTIME_OVERRIDE", "EFFECTIVE_HASH", "EXECUTION_HASH",
    "SELECTOR_CONTEXT", "LINUX_REQUIRED", "EXECUTION_CHANGED_DURING_COLLECTION", "PANEL_ACCEPT_TIMEOUT",
    "PANEL_PEER_NOT_VERIFIED", "PANEL_EXECUTION_CONTEXT_CHANGED", "PANEL_PEER_CHANGED"})


def diagnostic_error(error, reason=None):
    """Fixed enums only; never format exceptions or inspect HTTP/body/status attributes."""
    if type(error) is RuntimeError and type(reason) is str and reason in REASONS:
        return {"failureType": "RuntimeError", "errorCode": reason}
    families = ((execution.ExecutionUnavailable, "ExecutionUnavailable", "EXECUTION_UNAVAILABLE"),
                (intent_writer.IntentUnavailable, "IntentUnavailable", "INTENT_NOT_FROZEN"),
                (owner.OwnerUnavailable, "OwnerUnavailable", "OWNER_REPLY_NOT_VERIFIED"),
                (completion.CompletionUnavailable, "CompletionUnavailable", "COMPLETION_UNAVAILABLE"),
                (subprocess.TimeoutExpired, "TimeoutExpired", "SUBPROCESS_TIMEOUT"),
                (subprocess.CalledProcessError, "CalledProcessError", "SUBPROCESS_EXIT_NONZERO"),
                (AssertionError, "AssertionError", "ASSERTION_FAILED"),
                (sqlite3.Error, "SQLiteError", "SQLITE_ERROR"), (OSError, "OSError", "OS_ERROR"),
                (ValueError, "ValueError", "INVALID_VALUE"), (TypeError, "TypeError", "INVALID_TYPE"),
                (KeyError, "KeyError", "MISSING_FIELD"), (RuntimeError, "RuntimeError", "RUNTIME_ERROR"))
    for family, kind, code in families:
        if isinstance(error, family):
            if type(error) is execution.ExecutionUnavailable:
                # One exact source-defined collector enum, never a parsed/normalized message.
                args = BaseException.args.__get__(error, type(error))
                if len(args) == 1 and type(args[0]) is str and args[0] in COLLECTOR_CODES:
                    code = args[0]
            return {"failureType": kind, "errorCode": code}
    return {"failureType": "UnclassifiedError", "errorCode": "UNCLASSIFIED_ERROR"}


class PhaseDiagnostics:
    def __init__(self, clock=None):
        self.clock = time.monotonic if clock is None else clock
        self.phase, self.started = "PRECHECK", self.tick()
        self.durations, self.reason, self.stopped = {}, None, False

    def tick(self):
        try:
            value = self.clock()
            return value if type(value) in (int, float) and math.isfinite(value) else None
        except Exception:
            return None

    def elapsed(self):
        try:
            value = self.tick() - self.started
            return round(value, 6) if math.isfinite(value) and 0 <= value <= 1800 else None
        except (TypeError, ValueError, OverflowError):
            return None

    def enter(self, phase):
        if type(phase) is not str or phase not in PHASES or self.stopped:
            raise ValueError("UNSUPPORTED_DIAGNOSTIC_PHASE")
        self.durations[self.phase] = self.elapsed()
        self.phase, self.started, self.reason = phase, self.tick(), None

    def stop(self):
        if not self.stopped:
            self.durations[self.phase] = self.elapsed()
            self.stopped = True
        return {"phase": self.phase, "phaseSeconds": self.durations[self.phase],
                "phaseDurations": dict(self.durations)}

    def failure(self, error):
        return {**self.stop(), "failedPhase": self.phase, **diagnostic_error(error, self.reason)}


def digest_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def synthetic_inputs():
    """Real renderer/compiler shapes; no native identity or canned owner reply."""
    config, plan, template = fixture()
    engine = {"name": "xray", "version": "26.10.21", "sha256": execution.CORE}
    plan["channel"]["engine"] = engine
    for approval in plan["exits"]:
        approval["channel"]["engine"] = copy.deepcopy(engine)
    config, runtime, template, binding = channels.stage(config, {}, "route-auto-europe", plan, template, now=int(time.time()))
    route = next(route for route in config["routes"] if route["id"] == "route-auto-europe")
    payload = runtime_state.inbound_payload(config, route)
    assert payload["listen"] == "127.0.0.1" and payload["settings"]["clients"] == []
    assert route["presentation"]["published"] is False
    # Stock observatory's URL remains policy-owned; an isolated net namespace
    # prevents any outbound probe. Synthetic health below is never physical proof.
    return config, runtime, template, plan, binding, payload


def observation(plan, binding, view):
    return {"schema_version": 3, "binding": binding, "engine": plan["channel"]["engine"],
            "at": int(time.time()), "paths": {path["id"]: True for path in plan["paths"]},
            "engine_instance_before": copy.deepcopy(view["engine_instance"]),
            "engine_instance_after": copy.deepcopy(view["engine_instance"]),
            "effective_config_sha256": view["effective_config_sha256"],
            "native_generation_before": view["native_generation"], "native_generation_after": view["native_generation"],
            "mutation_epoch_before": view["mutation_epoch"], "mutation_epoch_after": view["mutation_epoch"]}


def run(producer, core, manifest):
    result = {"status": "FAILED_NOT_ACCEPTED", "ownerInvocations": 0, "physicalFailClosed": "NOT_RUN",
              "capability": False, "syntheticHealth": True, "producerSource": PRODUCER_SOURCE}
    diagnostics = PhaseDiagnostics()
    process = None
    log = None
    lock_fd = None
    owned = []
    transaction = None
    started = time.monotonic()
    try:
        assert sys.platform.startswith("linux") and os.geteuid() == 0
        import fcntl
        os.umask(0o077)  # Child-only, inherited by migrate/daemon/config writers.
        assert {name for _, name in socket.if_nameindex()} == {"lo"}
        for namespace in ("net", "mnt"):
            assert os.stat(f"/proc/self/ns/{namespace}").st_ino != os.stat(f"/proc/1/ns/{namespace}").st_ino
        subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert digest_file(producer) == PRODUCER and digest_file(core) == execution.CORE
        assert (producer, core, Path(manifest)) == (INPUT / "out_x-ui-private", INPUT / "xray-linux-amd64", INPUT / "NATIVE-INPUTS.json")
        manifest_raw = Path(manifest).read_bytes()
        frozen = reader.decode(manifest_raw)
        assert frozen["producerSha256"] == PRODUCER and frozen["coreSha256"] == execution.CORE
        assert frozen["syntheticOnly"] is True and frozen["producerSource"] == PRODUCER_SOURCE
        # Refuse existing paths/process ownership rather than clean foreign state.
        for path in (WORK, execution.APPROVAL.parent, execution.DIRECTORY,
                     completion.NODE_MUTATION_LOCK, Path("/var/log/wavemesh-node")):
            assert not os.path.lexists(path), "fixture destination already occupied"
        try:
            os.fstat(9)
        except OSError:
            pass
        else:
            diagnostics.reason = "FD9_OCCUPIED"
            raise RuntimeError("FD9 occupied")
        diagnostics.enter("FIXTURE_PREPARATION")
        ownership = INPUT / "FIXTURE-OWNERSHIP.json"
        assert not os.path.lexists(ownership)
        # Durable write-ahead ownership precedes even mkdir/copy/migrate. A
        # terminal failed driver can leave partial files without a first result.
        journal.atomic_json(ownership, {"schema_version": 1, "manifestSha256": reader.sha(manifest_raw),
                            "producerSha256": PRODUCER, "coreSha256": execution.CORE,
                            "driver": execution._process(os.getpid()), "phase": "FRESH_PATHS_RESERVED",
                            "freshAbsentPaths": [str(path) for path in (WORK, execution.APPROVAL.parent,
                                execution.DIRECTORY, completion.NODE_MUTATION_LOCK, Path("/var/log/wavemesh-node"))]})
        WORK.mkdir(mode=0o700)
        owned.append(WORK)
        for name in ("bin", "db", "home"):
            (WORK / name).mkdir(mode=0o700)
        private_log = Path("/var/log/wavemesh-node")
        private_log.mkdir(mode=0o700)
        owned.append(private_log)
        shutil.copyfile(core, WORK / "bin/xray-linux-amd64")
        (WORK / "bin/xray-linux-amd64").chmod(0o500)
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(WORK / "home"), "LANG": "C.UTF-8",
               "XUI_DB_FOLDER": str(WORK / "db"), "XUI_BIN_FOLDER": str(WORK / "bin"),
               "XUI_PREPARED_ATTESTATION": "1", "NODE_TOKEN_ENCRYPTION": "off",
               "XUI_LOG_FOLDER": "/var/log/wavemesh-node"}
        log = (WORK / "producer.log").open("wb")
        diagnostics.enter("MIGRATION")
        subprocess.run([str(producer), "migrate"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                       stdout=log, stderr=log, timeout=60, check=True)
        diagnostics.enter("DATABASE_FIXTURE")
        database = WORK / "db/x-ui.db"
        database.chmod(0o600)
        config, runtime, template, plan, binding, payload = synthetic_inputs()
        token = secrets.token_urlsafe(32)
        settings = {"webListen": "127.0.0.1", "webPort": "21853", "webBasePath": "/fixture/",
                    "webCertFile": "", "webKeyFile": "", "webDomain": "", "tgBotEnable": "false",
                    "discordBotEnable": "false", "smtpEnable": "false", "ldapEnable": "false",
                    "subEnable": "false", "subJsonEnable": "false", "subClashEnable": "false",
                    "warpUpdateInterval": "0", "subRoutingRules": "", "subClashRules": "",
                    "subJsonRoutingRules": "", "xrayTemplateConfig": json.dumps(template, ensure_ascii=False)}
        with closing(sqlite3.connect(database)) as db:
            # A normal user row prevents startup's first-user credential output.
            db.execute("DELETE FROM users")
            db.execute("INSERT INTO users(id,username,password,login_epoch) VALUES (1,?,?,0)",
                       ("synthetic-disabled-login", "!no-login-in-this-fixture"))
            db.execute("DELETE FROM settings")
            db.executemany("INSERT INTO settings(key,value) VALUES (?,?)", settings.items())
            db.execute("DELETE FROM api_tokens")
            db.execute("INSERT INTO api_tokens(name,token,enabled,scope,expires_at,created_at) VALUES (?,?,1,'admin',0,?)",
                       ("synthetic-composition-only", reader.sha(token.encode()), int(time.time() * 1000)))
            db.execute("INSERT INTO inbounds(id,user_id,enable,listen,port,protocol,settings,stream_settings,tag,sniffing) VALUES (77,1,1,?,?,?,?,?,?,?)",
                       (payload["listen"], payload["port"], payload["protocol"], json.dumps(payload["settings"]),
                        json.dumps(payload["streamSettings"]), payload["tag"], json.dumps(payload["sniffing"])))
            for table in ("nodes", "clients", "outbound_subscriptions"):
                # Exact table names reviewed before native admission; missing
                # tables are a source/preflight gap, never silently ignored.
                assert db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
            db.commit()
        execution.APPROVAL.parent.mkdir(mode=0o700)
        owned.append(execution.APPROVAL.parent)
        journal.atomic_json(execution.APPROVAL, {"schema_version": 1, "artifact_kind": "full-producer",
                            "source_commit": PRODUCER_SOURCE, "upstream_commit": execution.UPSTREAM,
                            "producer_sha256": PRODUCER, "core_sha256": execution.CORE,
                            "release_manifest_sha256": reader.sha(manifest_raw)})
        lock_fd = os.open(completion.NODE_MUTATION_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        if lock_fd == 9:
            lock_fd = os.dup(lock_fd)
        os.dup2(lock_fd, 9)
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owned.append(execution.DIRECTORY)  # Only our fresh Producer can create it.
        diagnostics.enter("PRODUCER_START")
        process = subprocess.Popen([str(producer), "run"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        result["producerPid"] = process.pid
        reference = {"producer": execution._process(process.pid), "session": process.pid}
        journal.atomic_json(WORK / "process-reference.json", reference)
        # Observe SAME original process; never relaunch after readiness failure.
        diagnostics.enter("SNAPSHOT_WAIT")
        deadline = time.monotonic() + 60
        while not execution.SNAPSHOT.exists():
            producer_status = process.poll()
            if producer_status is not None or time.monotonic() >= deadline:
                diagnostics.reason = "PRODUCER_EARLY_EXIT" if producer_status is not None else "SNAPSHOT_WAIT_EXPIRED"
                raise RuntimeError("original Producer not ready")
            time.sleep(0.1)
        diagnostics.enter("INITIAL_COLLECT")
        collection_started = time.monotonic()
        view, effective = execution.collect()
        result["initialCollectionSeconds"] = time.monotonic() - collection_started
        reference["core"] = execution._process(view["engine_instance"]["pid"])
        journal.atomic_json(WORK / "process-reference.json", reference)
        result["initialGeneration"], result["initialEpoch"] = view["native_generation"], view["mutation_epoch"]
        assert view["mutation_epoch"] == "0"
        first = observation(plan, binding, view)
        fingerprint = channels.health_config_fingerprint(config, "route-auto-europe", plan, effective, view)
        runtime["prepared_routes"]["route-auto-europe"] = channels.select_paths(
            plan, binding, {}, first, first["at"], view, fingerprint, "wm-rule-auto-europe")
        runtime["unowned_sentinel"] = {"preserve": True}
        paths = {"config": str(WORK / "config.json"), "runtime": str(WORK / "runtime.json"),
                 "database": str(database), "process": str(WORK / "bin/config.json")}
        journal.atomic_json(paths["config"], config)
        journal.atomic_json(paths["runtime"], runtime)
        credentials = WORK / "synthetic-bearer.json"
        journal.atomic_json(credentials, {"schema_version": 1, "bearer_token": token})
        del token
        # Preserve the actual 10s hysteresis interval, without changing clocks.
        diagnostics.enter("HEALTH_WAIT")
        while int(time.time()) < first["at"] + plan["health"]["sample_interval_seconds"]:
            assert process.poll() is None
            time.sleep(0.1)
        diagnostics.enter("SECOND_COLLECT")
        second_view, _ = execution.collect()
        assert second_view == view
        samples = observation(plan, binding, view)
        diagnostics.enter("INTENT_PREPARE")
        transaction = journal.begin(WORK / "transactions", "auto-prepared-reconcile", os.getpid())
        intent = intent_writer.prepare_once(transaction, "route-auto-europe", paths, samples)
        result["transactionId"], result["intentSha256"] = transaction.name, reader.sha((transaction / "prepared.apply.pending.json").read_bytes())
        result["ownerInvocations"] = 1
        diagnostics.enter("OWNER_API")
        owner_started = time.monotonic()
        audit = {"active": True, "connect": None, "header": None, "sends": 0}
        def observe(event, arguments):
            # CPython metadata-only audit observation. Never inspect arguments:
            # http.client.send arguments contain the private request bytes.
            if not audit["active"]:
                return
            if event == "socket.connect" and audit["connect"] is None:
                audit["connect"] = time.monotonic()
            elif event == "http.client.send":
                if audit["header"] is None:
                    audit["header"] = time.monotonic()
                audit["sends"] += 1
        sys.addaudithook(observe)
        # No retry/restore/compensation on failure or lost reply.
        try:
            assert owner.apply_once(transaction, credentials) == "committed"
        finally:
            audit["active"] = False
            result["ownerSeconds"] = time.monotonic() - owner_started
            result["httpSendAuditEvents"] = audit["sends"]
            result["connectToHeaderAttemptSeconds"] = (None if audit["header"] is None or audit["connect"] is None
                                                       else audit["header"] - audit["connect"])
        assert audit["connect"] is not None and audit["header"] is not None
        diagnostics.enter("FINAL_COLLECT")
        final_view, final_effective = execution.collect()
        diagnostics.enter("POSTIMAGE_VERIFY")
        assert final_view["native_generation"] == view["native_generation"]
        assert final_view["mutation_epoch"] == "1"
        assert final_view["effective_config_sha256"] == intent["expected"]["after_sha256"]
        assert final_view["selector_transitions"] == [reader.expected_transition(intent)]
        assert reader.selector_matches(final_effective, intent["expected"])
        assert reader.prepared_status(transaction) == "committed"
        assert reader.decode(Path(paths["runtime"]).read_bytes())["unowned_sentinel"] == {"preserve": True}
        reply = reader.decode((transaction / "prepared.owner-result.json").read_bytes())
        assert reader.known_owner_result(reply)
        assert digest_file(paths["process"]) == reply["outcome"]["ProcessConfigSHA256"]
        result.update(status="PASS_FULL_SYNTHETIC_COMPOSITION_ONLY", nativeEpochAdvance=1,
                      actualEffectiveSha256=final_view["effective_config_sha256"],
                      processPostimageSha256=reply["outcome"]["ProcessConfigSHA256"])
    except Exception as error:
        result.update(diagnostics.failure(error))
    finally:
        result.update(diagnostics.stop())
        result["seconds"] = time.monotonic() - started
        result["dispatchRecorded"] = transaction is not None and os.path.lexists(transaction / owner.DISPATCH)
        result["availableExports"] = (["process-reference.json"] if WORK in owned and (WORK / "process-reference.json").is_file() else [])
        # Preserve result and transaction debt before any cleanup. Exporting
        # original receipts/owned files and independent cleanup is outer-driver work.
        def preserve_result():
            if WORK in owned:
                try:
                    journal.atomic_json(WORK / "result.json", result)
                except OSError:
                    result["status"] = "FAILED_RESULT_DURABILITY"
        preserve_result()
        if process is not None:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            result["producerTerminal"] = process.returncode
        if lock_fd is not None:
            os.close(9)
            os.close(lock_fd)
        if log is not None:
            log.close()
        # Deliberately retain owned paths, synthetic DB/token and original debt
        # for bounded outer export/cleanup. No secret-bearing file is exported.
        result["ownedPaths"] = [str(path) for path in owned]
        preserve_result()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS_FULL_SYNTHETIC_COMPOSITION_ONLY" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--producer", required=True, type=Path)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.producer.resolve(), args.core.resolve(), args.manifest.resolve()))
