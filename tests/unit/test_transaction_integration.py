from pathlib import Path

root = Path(__file__).resolve().parents[2]
commands = {
    name: (root / "scripts" / "commands" / name).read_text(encoding="utf-8")
    for name in ("cascade.sh", "exit_peer.sh", "runtime.sh", "subscription.sh")
}
combined = "\n".join(commands.values())

assert "wm_transaction_dir" not in combined
assert 'result.json"' not in combined

expected = {
    "cascade.sh": ("cascade-add-exit",),
    "exit_peer.sh": ("exit-peer-create", "exit-peer-remove"),
    "runtime.sh": ("route-${operation}", "route-remove", "cascade-remove-exit", "reconcile-apply"),
    "subscription.sh": ("subscription-rebuild", "subscription-rotate-path", "subscription-migrate-native", "subscription-fallback-generated"),
}
for name, operations in expected.items():
    text = commands[name]
    for operation in operations:
        assert f'wm_lock_mutation "{operation}"' in text
        assert f'wm_transaction_begin "{operation}"' in text
    assert text.count("wm_transaction_commit") >= len(operations)

cli = (root / "bin" / "wavemesh").read_text(encoding="utf-8")
assert "wavemesh transaction list [--json]" in cli
assert "wavemesh transaction recover --id ID|--latest" in cli
assert cli.count('source "$WM_LIB_DIR/lib/transaction.sh"') == 6

print("transaction integration tests: OK")
