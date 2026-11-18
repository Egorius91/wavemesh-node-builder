"""Private, unwired one-send owner caller. No CLI, login or generic activation.

Only the admitted node-lock owner may provide a frozen intent and an existing
private bearer file. A durable dispatch fact prevents replay after any failure.
"""
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import time

import prepared_completion as completion
import prepared_execution as execution
import prepared_journal as reader
import transaction_state as journal

DISPATCH = "prepared.owner-request.json"
DIAGNOSTIC = "X-WaveMesh-Execution-Diagnostic"


class OwnerUnavailable(RuntimeError):
    """Fixed diagnostic with no credential, request, database or wire contents."""


def _admission(path, token):
    # Reuse the same private SELECT-only connection and exact stored row reader.
    # The transaction is closed BEFORE any network send, with no writer lock.
    with completion._database_reader(path) as database:
        names = ("webListen", "webPort", "webBasePath", "webCertFile", "webKeyFile")
        settings = {}
        for name in names:
            rows = database.execute("SELECT value FROM settings WHERE key=? LIMIT 2", (name,)).fetchall()
            if len(rows) != 1 or not isinstance(rows[0][0], str) or len(rows[0][0]) > 256:
                raise ValueError("explicit existing panel location required")
            settings[name] = rows[0][0]
        if settings["webListen"] != "127.0.0.1" or settings["webCertFile"] or settings["webKeyFile"]:
            raise ValueError("explicit literal loopback HTTP panel required")
        port = int(settings["webPort"])
        prefix = settings["webBasePath"]
        if (str(port) != settings["webPort"] or not 1 <= port <= 65535
                or not re.fullmatch(r"/(?:[A-Za-z0-9_~-]+/)*", prefix)):
            raise ValueError("canonical existing panel location required")
        rows = database.execute("SELECT enabled,scope,expires_at FROM api_tokens WHERE token=? LIMIT 2", (reader.sha(token.encode()),)).fetchall()
        if (len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != "admin" or type(rows[0][2]) is not int
                or rows[0][2] < 0 or rows[0][2] != 0 and int(time.time()*1000) >= rows[0][2]):
            raise ValueError("existing explicit enabled admin authority required")
        return port, prefix + "panel/api/xray/executionSelector", completion._template_row(database)


def apply_once(transaction, credential_file):
    """Retain one trusted reply then use existing completion; never replay.

    No ordinary command imports this module. A pre-existing dispatch fact is
    always a refusal here; known-reply metadata resumption belongs to complete.
    """
    connection = None
    response = None
    try:
        completion._require_node_owner()
        directory = Path(transaction)
        if (directory.is_symlink() or os.path.lexists(directory / DISPATCH)
                or os.path.lexists(directory / "prepared.owner-result.json")
                or os.path.lexists(directory / "prepared.apply.completed.json")):
            raise ValueError("one new pending operation required")
        plan = reader.decode(reader.read_private(directory / "plan.json"))
        if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1 or plan.get("operation") != "auto-prepared-reconcile":
            raise ValueError("private reconcile owner required")
        intent_raw = reader.read_private(directory / "prepared.apply.pending.json")
        intent = reader.validate_intent(reader.decode(intent_raw), directory.name)
        candidates = {}
        for kind, filename in reader.ARTIFACTS.items():
            raw = reader.read_private(directory / filename)
            if reader.sha(raw) != intent["candidate"][kind]:
                raise ValueError("frozen candidate changed")
            candidates[kind] = reader.decode(raw)
        completion._approved_plan(intent, candidates["config"])
        if (not reader.selector_matches(candidates["template"], intent["expected"])
                or reader.sha(reader.read_private(intent["files"]["config"])) != intent["before"]["config"]):
            raise ValueError("frozen selector or local preimage changed")
        _, runtime = completion._optional(intent["files"]["runtime"])
        if completion._owned(runtime, intent["route_id"]) != intent["before"]["owned"]:
            raise ValueError("local route preimage changed")
        credentials = reader.decode(reader.read_private(credential_file))
        reader._shape(credentials, ("schema_version", "bearer_token"))
        token = credentials["bearer_token"]
        if type(credentials["schema_version"]) is not int or credentials["schema_version"] != 1 or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,512}={0,2}", token):
            raise ValueError("existing private bearer required")
        port, path, template = _admission(intent["files"]["database"], token)
        if reader.sha(template.encode()) != intent["before"]["template"]:
            raise ValueError("exact stored template preimage changed")
        expected = intent["expected"]
        request = {"schema_version": 1, "transaction_id": directory.name, "intent_sha256": reader.sha(intent_raw),
                   "expected": {"generation": expected["generation"], "epoch": expected["epoch"], "effective_config_sha256": expected["before_sha256"]},
                   "template_before": template, "rule_tag": expected["rule_tag"], "target": expected["target"]}
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > reader.LIMIT:
            raise ValueError("bounded request required")
        # Persist BEFORE connection/request: any crash or ambiguous write keeps
        # debt. Neither this function nor transport may manufacture a retry.
        journal.atomic_json(directory / DISPATCH, {"schema_version": 1, "transaction_id": directory.name,
                            "intent_sha256": request["intent_sha256"], "request_sha256": reader.sha(body)})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=35)
        connection.set_debuglevel(0)
        # AF_INET numeric connect avoids getaddrinfo/DNS entirely. Disallow the
        # HTTP helper's implicit connect path, including any later reopening.
        connection.auto_open = 0
        connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.sock.settimeout(35)
        connection.sock.connect(("127.0.0.1", port))
        # No HTTP/Authorization bytes until private custody has authenticated
        # the same connected peer's accepted inode/FD and stable process.
        with execution.verified_panel_socket(connection.sock, expected):
            connection.request("POST", path, body=body, headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            response = connection.getresponse()
            raw = response.read(reader.LIMIT + 1)
        diagnostics = [value for key, value in response.getheaders() if key.lower() == DIAGNOSTIC.lower()]
        if (len(raw) > reader.LIMIT or len(diagnostics) != 1 or response.status not in (200, 409)
                or len(response.headers.get_all('Content-Type', [])) != 1 or response.headers.get_content_type() != 'application/json'):
            raise ValueError("bounded owner response required")
        reply = reader.validate_owner_result(reader.decode(raw), directory.name, request["intent_sha256"])
        known = reader.known_owner_result(reply)
        if not ((response.status == 200 and diagnostics == ["COMMITTED"] and known)
                or (response.status == 200 and diagnostics == ["NO_OP"] and reply["outcome"]["NoOp"] is True and not known)
                or (response.status == 409 and diagnostics == ["OUTCOME_UNPROVEN"] and not known)):
            raise ValueError("owner status and result contradict")
        completion.record_owner_result(directory, reply)
        if known:
            return completion.complete(directory)
        return "pending"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, StopIteration, sqlite3.Error, http.client.HTTPException, completion.CompletionUnavailable, execution.ExecutionUnavailable):
        raise OwnerUnavailable("PREPARED_OWNER_REPLY_NOT_VERIFIED") from None
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
