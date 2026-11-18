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
    process = None
    log = None
    lock_fd = None
    owned = []
    transaction = None
    started = time.monotonic()
    try:
        assert sys.platform.startswith("linux") and os.geteuid() == 0
        import fcntl
        assert {name for _, name in socket.if_nameindex()} == {"lo"}
        for namespace in ("net", "mnt"):
            assert os.stat(f"/proc/self/ns/{namespace}").st_ino != os.stat(f"/proc/1/ns/{namespace}").st_ino
        subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert digest_file(producer) == PRODUCER and digest_file(core) == execution.CORE
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
            raise RuntimeError("FD9 occupied")
        WORK.mkdir(mode=0o700)
        owned.append(WORK)
        for name in ("bin", "db", "home"):
            (WORK / name).mkdir(mode=0o700)
        shutil.copyfile(core, WORK / "bin/xray-linux-amd64")
        (WORK / "bin/xray-linux-amd64").chmod(0o500)
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(WORK / "home"), "LANG": "C.UTF-8",
               "XUI_DB_FOLDER": str(WORK / "db"), "XUI_BIN_FOLDER": str(WORK / "bin"),
               "XUI_PREPARED_ATTESTATION": "1", "NODE_TOKEN_ENCRYPTION": "off"}
        log = (WORK / "producer.log").open("wb")
        subprocess.run([str(producer), "migrate"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                       stdout=log, stderr=log, timeout=60, check=True)
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
        private_log = Path("/var/log/wavemesh-node")
        private_log.mkdir(mode=0o700)
        owned.append(private_log)
        lock_fd = os.open(completion.NODE_MUTATION_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        if lock_fd == 9:
            lock_fd = os.dup(lock_fd)
        os.dup2(lock_fd, 9)
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owned.append(execution.DIRECTORY)  # Only our fresh Producer can create it.
        process = subprocess.Popen([str(producer), "run"], cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        result["producerPid"] = process.pid
        reference = {"producer": execution._process(process.pid), "session": process.pid}
        journal.atomic_json(WORK / "process-reference.json", reference)
        # Observe SAME original process; never relaunch after readiness failure.
        deadline = time.monotonic() + 60
        while not execution.SNAPSHOT.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("original Producer not ready")
            time.sleep(0.1)
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
        while int(time.time()) < first["at"] + plan["health"]["sample_interval_seconds"]:
            assert process.poll() is None
            time.sleep(0.1)
        second_view, _ = execution.collect()
        assert second_view == view
        samples = observation(plan, binding, view)
        transaction = journal.begin(WORK / "transactions", "auto-prepared-reconcile", os.getpid())
        intent = intent_writer.prepare_once(transaction, "route-auto-europe", paths, samples)
        result["transactionId"], result["intentSha256"] = transaction.name, reader.sha((transaction / "prepared.apply.pending.json").read_bytes())
        result["ownerInvocations"] = 1
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
        final_view, final_effective = execution.collect()
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
        # Fixed type only. Never emit config/SQL/wire/credentials/raw exception.
        result["failureType"] = type(error).__name__
    finally:
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
