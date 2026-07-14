# Opaque subscription paths

New installations no longer expose the fixed `/sub/` prefix. The installer generates a two-segment opaque path such as:

```text
/q8Kp2RmXn4/Dt7Vb3Ls9Qc6Hw2ZaP/
```

The full URL remains a bearer secret. Randomizing the path reduces trivial discovery and common-path fingerprinting, but it does not replace access control or safe handling of subscription links.

## Existing servers

Preview a new path without changing nginx, configuration, or client links:

```bash
sudo wavemesh subscription rotate-path --dry-run
```

Apply a transactional rotation:

```bash
sudo wavemesh subscription rotate-path --apply
```

The command renders subscriptions, applies nginx, validates the new public URL, commits configuration only after successful validation, and prints the new client URL. The old URL is removed after a successful commit, so update the bot and clients immediately.

A specific opaque path may be supplied:

```bash
sudo wavemesh subscription rotate-path --apply --path /q8Kp2RmXn4/Dt7Vb3Ls9Qc6Hw2ZaP/
```

Paths beginning with `/sub/` are rejected.
