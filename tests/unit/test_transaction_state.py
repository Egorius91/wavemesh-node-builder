import importlib.util
import json
import os
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tool = root / "scripts" / "lib" / "transaction_state.py"
spec = importlib.util.spec_from_file_location("transaction_state", tool)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as name:
    state = Path(name) / "transactions"
    first = module.begin(state, "route-remove secret-must-not-appear", 123)
    assert first.parent == state
    if os.name != "nt":
        assert first.stat().st_mode & 0o777 == 0o700
        assert (first / "plan.json").stat().st_mode & 0o777 == 0o600
    assert module.incomplete(state)[0]["id"] == first.name

    try:
        module.begin(state, "second", 124)
    except RuntimeError as error:
        assert first.name in str(error)
    else:
        raise AssertionError("incomplete transaction did not block a new mutation")

    assert module.resolve(state, first.name, active_only=True) == first
    for unsafe in ("../escape", first.name + "/child", ""):
        try:
            module.resolve(state, unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe transaction id was accepted")

    module.mark(first, "rolled_back", "operator recovery")
    assert not module.incomplete(state)
    try:
        module.resolve(state, first.name, active_only=True)
    except ValueError:
        pass
    else:
        raise AssertionError("terminal transaction was recoverable")

    created = []
    for index in range(4):
        transaction = module.begin(state, f"operation-{index}", 200 + index)
        module.mark(transaction, "committed")
        created.append(transaction)
    active = module.begin(state, "interrupted", 999)
    module.prune(state, 2)
    remaining = {item["id"]: item for item in module.records(state)}
    assert active.name in remaining and remaining[active.name]["status"] == "in_progress"
    assert sum(item["status"] in module.TERMINAL for item in remaining.values()) == 2

    backups = Path(name) / "backups"
    backups.mkdir()
    for index in range(5):
        path = backups / f"xray.{index}.json"
        path.write_text(json.dumps({"index": index}), encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
    module.prune_backups(backups, 2)
    assert {item.name for item in backups.iterdir()} == {"xray.3.json", "xray.4.json"}

    source = Path(name) / "candidate.json"
    target = Path(name) / "installed.json"
    source.write_text('{"value": 7}', encoding="utf-8")
    module.atomic_json(target, json.loads(source.read_text(encoding="utf-8")))
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 7}

print("transaction state tests: OK")
