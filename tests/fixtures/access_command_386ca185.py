# Frozen validate_access_command from 386ca185c3cfc4021581788f003a745f9ae1e4ac agent/node_agent.py.
# Its globals are supplied from the unchanged shared validation helpers by the test.
def validate_access_command(
    command: dict[str, Any],
    node_id: str,
    tenant_id: str | None = None,
) -> tuple[str, int, str, dict[str, Any]]:
    command_type = command.get("type")
    if command_type not in {
        "access.provision",
        "access.replace_credential",
        "access.update_entitlements",
        "access.prepare_replacement",
    }:
        raise AgentError("Unsupported Node command type")
    schema = command.get("schema_version")
    if command.get("target_node_id") != node_id or type(schema) is not int or schema not in {1, 2, 3}:
        raise AgentError("Node command target or schema is invalid")
    command_id = safe_id(command.get("command_id"), "command_id")
    attempt = safe_int(command.get("attempt"), 1, 1_000_000)
    if attempt != command.get("attempt"):
        raise AgentError("Node command attempt is invalid")
    payload = command.get("payload")
    required = {
        "access_id",
        "desired_version",
        "enabled",
        "expires_at",
        "device_limit",
        "quota_bytes",
    }
    if command_type == "access.prepare_replacement":
        required.add("replacement_id")
    if schema in {2, 3}:
        if command_type == "access.prepare_replacement" or not tenant_id or command.get("tenant_id") != tenant_id:
            raise AgentError("Placement command tenant or operation is invalid")
        required.update({"placement_id", "generation", "entitlement_revision", "plan_hash", "command_id"})
        if schema == 3:
            required.add("traffic_budget")
    if not isinstance(payload, dict) or set(payload) != required:
        raise AgentError("Node command payload is invalid")
    if command_type == "access.prepare_replacement":
        safe_id(payload.get("replacement_id"), "replacement_id")
        if payload.get("enabled") is not False:
            raise AgentError("Replacement preparation must be disabled")
    if type(payload.get("enabled")) is not bool or (
        payload["enabled"] is False and command_type not in {"access.update_entitlements", "access.prepare_replacement"}
    ):
        raise AgentError("Access enabled state is invalid for this operation")
    safe_id(payload.get("access_id"), "access_id")
    desired_version = safe_int(payload.get("desired_version"), 1, 2_147_483_647)
    if desired_version != payload.get("desired_version"):
        raise AgentError("Access desired version is invalid")
    parse_timestamp(require_string(payload, "expires_at"))
    if isinstance(payload.get("device_limit"), bool):
        raise AgentError("Access device limit is invalid")
    if safe_int(payload.get("device_limit"), 0, 10_000) != payload.get("device_limit"):
        raise AgentError("Access device limit is invalid")
    quota = payload.get("quota_bytes")
    if not isinstance(quota, str) or not quota.isdigit() or int(quota) > 9_223_372_036_854_775_807:
        raise AgentError("Access quota is invalid")
    result = dict(payload)
    if schema in {2, 3}:
        if any(type(value) is not int for value in (command.get("attempt"), payload.get("desired_version"), payload.get("device_limit"))):
            raise AgentError("Placement command integer fields are invalid")
        placement = payload.get("placement_id")
        if not isinstance(placement, str) or not re.fullmatch(r"(?:legacy:)?[A-Za-z0-9_-]{1,128}", placement):
            raise AgentError("Placement command identity is invalid")
        for name in ("generation", "entitlement_revision"):
            value = payload.get(name)
            if type(value) is not int or not 1 <= value <= 2_147_483_647:
                raise AgentError("Placement command revision is invalid")
        if payload.get("command_id") != command_id or not isinstance(payload.get("plan_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", payload["plan_hash"]):
            raise AgentError("Placement command binding is invalid")
        expected = command.get("expected_version")
        if type(expected) is not int or not 0 <= expected < desired_version:
            raise AgentError("Placement command expected version is invalid")
        result.update(schema_version=schema, tenant_id=tenant_id, node_id=node_id, expected_version=expected)
        if schema == 3:
            budget = validate_traffic_budget(payload["traffic_budget"])
            cap = traffic_bytes(quota)
            if payload["enabled"] and (budget is None and cap != 0 or budget is not None and (cap == 0 or traffic_bytes(budget["credit_bytes"]) > cap)):
                raise AgentError("Enabled finite access requires bounded reserved credit")
    return command_id, attempt, str(command_type), result
