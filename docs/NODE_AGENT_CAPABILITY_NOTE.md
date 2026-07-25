# Node Agent Xray process detection capability

The observe-only Node Agent runs inside a hardened systemd sandbox. WaveMesh route health checks identify the running Xray process through `/proc/<pid>/exe`.

With an empty `CapabilityBoundingSet`, that process inspection can fail inside the sandbox even while Xray and all managed routes are healthy. The service therefore permits only:

```ini
CapabilityBoundingSet=CAP_SYS_PTRACE
```

No network-administration, filesystem-administration, or service-mutation capabilities are granted. `NoNewPrivileges=true` and the remaining systemd hardening settings stay enabled.
