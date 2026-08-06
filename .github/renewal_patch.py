from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"anchor mismatch: {path}: {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


agent = "agent/node_agent.py"
replace_once(
    agent,
    'AGENT_VERSION = "0.4.0-access-lifecycle"',
    'AGENT_VERSION = "0.5.0-access-entitlements"',
)
replace_once(
    agent,
    '            material = self.execute_access_runtime(payload)\n',
    '            material = self.execute_access_runtime(command_type, payload)\n',
)
replace_once(
    agent,
    '''    def execute_access_runtime(self, payload: dict[str, Any]) -> dict[str, Any]:
''',
    '''    def execute_access_runtime(
        self,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
''',
)
replace_once(
    agent,
    '''            output_path = Path(directory) / "material.json"
            write_json_file(request_path, payload)
            completed = subprocess.run(
''',
    '''            output_path = Path(directory) / "material.json"
            write_json_file(
                request_path,
                {**payload, "operation": command_type},
            )
            completed = subprocess.run(
''',
)
replace_once(
    agent,
    '''    if command_type not in {"access.provision", "access.replace_credential"}:
''',
    '''    if command_type not in {
        "access.provision",
        "access.replace_credential",
        "access.update_entitlements",
    }:
''',
)

runtime = "agent/access_runtime.py"
replace_once(
    runtime,
    '"""Idempotent local 3X-UI access provisioning for the WaveMesh Node Agent."""',
    '"""Idempotent local 3X-UI access lifecycle for the WaveMesh Node Agent."""',
)
replace_once(
    runtime,
    '''\ndef cleanup_previous(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> int:
''',
    r'''
def update_entitlements(
    request_value: dict[str, Any],
    config: dict[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    """Update expiry and limits while preserving the accepted access identity."""
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 2, 2_147_483_647)
    expires_at = parse_time(request_value.get("expires_at"))
    device_limit = integer(request_value.get("device_limit"), 0, 10_000)
    quota_bytes = integer(request_value.get("quota_bytes"), 0, 9_223_372_036_854_775_807)
    if request_value.get("enabled") is not True:
        raise ProvisionError("Disabled entitlement updates are unsupported")

    state_path = state_root / f"{access_id}.{desired_version}.json"
    state = load_private_state(state_path)
    if state is None:
        previous = latest_previous_state(state_root, access_id, desired_version)
        state = {
            "access_id": access_id,
            "desired_version": desired_version,
            "panel_email": previous["panel_email"],
            "client_uuid": previous["client_uuid"],
            "sub_id": previous["sub_id"],
        }
        atomic_json(state_path, state)
    validate_state(state, access_id, desired_version)

    subscription = object_value(object_value(config.get("network")).get("subscription"))
    if subscription.get("backend") != "xui-native":
        raise ProvisionError("xui-native subscription backend is required")
    domain = str(object_value(config.get("server")).get("domain") or "")
    sub_path = "/" + str(subscription.get("path") or "").strip("/") + "/"
    if not domain or ".." in sub_path or len(sub_path) < 12:
        raise ProvisionError("Native subscription settings are invalid")

    panel = PanelClient(config)
    inbound_ids = visible_vless_inbound_ids(panel.call("GET", "/panel/api/inbounds/list"))
    if not inbound_ids:
        raise ProvisionError("No public VLESS inbound is available")
    email = str(state["panel_email"])
    existing = get_client(panel, email)
    assert_matching_client(existing, state, inbound_ids)

    raw_client = existing.get("client") if isinstance(existing, dict) else None
    client = dict(raw_client if isinstance(raw_client, dict) else existing or {})
    client.pop("uuid", None)
    client.pop("inboundIds", None)
    client.update(
        {
            "email": email,
            "limitIp": device_limit,
            "totalGB": quota_bytes,
            "expiryTime": int(expires_at.timestamp() * 1000),
            "enable": True,
            "tgId": integer(client.get("tgId") or 0, 0, 9_223_372_036_854_775_807),
            "subId": state["sub_id"],
            "reset": integer(client.get("reset") or 0, 0, 2_147_483_647),
            "id": state["client_uuid"],
            "flow": str(client.get("flow") or ""),
        }
    )
    panel.call(
        "POST",
        f"/panel/api/clients/update/{parse.quote(email, safe='')}",
        client,
    )
    verified = get_client(panel, email)
    assert_matching_client(verified, state, inbound_ids)
    assert_entitlements(
        verified,
        expires_at_ms=int(expires_at.timestamp() * 1000),
        device_limit=device_limit,
        quota_bytes=quota_bytes,
    )
    links = panel.call(
        "GET",
        f"/panel/api/clients/subLinks/{parse.quote(str(state['sub_id']), safe='')}",
    )
    if not subscription_links_ready(links):
        raise ProvisionError("Native subscription links are not ready")

    return {
        "desired_version": desired_version,
        "panel_email": email,
        "client_uuid": state["client_uuid"],
        "sub_id": state["sub_id"],
        "primary_inbound_id": inbound_ids[0],
        "protocol": "vless",
        "subscription_url": f"https://{domain}{sub_path}{state['sub_id']}",
    }


def latest_previous_state(
    state_root: Path,
    access_id: str,
    desired_version: int,
) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for state_path in state_root.glob(f"{access_id}.*.json"):
        state = load_private_state(state_path)
        if state is None or state.get("access_id") != access_id:
            continue
        version = integer(state.get("desired_version"), 1, 2_147_483_647)
        if version >= desired_version:
            continue
        validate_state(state, access_id, version)
        candidates.append((version, state))
    if not candidates:
        raise ProvisionError("Previous durable access identity is missing")
    return max(candidates, key=lambda item: item[0])[1]


def assert_entitlements(
    record: dict[str, Any] | None,
    *,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> None:
    if not record:
        raise ProvisionError("3X-UI entitlement verification failed")
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    if (
        client.get("enable") is not True
        or integer(client.get("expiryTime"), 0, 9_223_372_036_854_775_807) != expires_at_ms
        or integer(client.get("limitIp"), 0, 10_000) != device_limit
        or integer(client.get("totalGB"), 0, 9_223_372_036_854_775_807) != quota_bytes
    ):
        raise ProvisionError("3X-UI entitlement update was not verified")


def cleanup_previous(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> int:
''',
)
replace_once(
    runtime,
    '''        if args.output is None:
            raise ProvisionError("output is required for provisioning")
        result = provision(command, config, args.state_root)
        atomic_json(args.output, result)
        print("access_provision=PASS")
''',
    '''        if args.output is None:
            raise ProvisionError("output is required for access lifecycle execution")
        operation = str(command.get("operation") or "access.provision")
        if operation == "access.update_entitlements":
            result = update_entitlements(command, config, args.state_root)
        elif operation in {"access.provision", "access.replace_credential"}:
            result = provision(command, config, args.state_root)
        else:
            raise ProvisionError("Unsupported access lifecycle operation")
        atomic_json(args.output, result)
        print(f"access_runtime=PASS operation={operation}")
''',
)
replace_once(
    runtime,
    '''    except Exception as exc:
        print(f"access_provision=FAIL code={type(exc).__name__.upper()}")
''',
    '''    except Exception as exc:
        print(f"access_runtime=FAIL code={type(exc).__name__.upper()}")
''',
)
