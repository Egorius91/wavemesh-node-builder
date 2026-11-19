# Optional public placement profile observation

This source component collects configuration-bound native client profiles for
the stable SaaS subscription. It is not executing TLS/egress or VPN acceptance.
It never provisions a client, resets counters, issues credit or rewrites material2/3.
It does not qualify a prepared PR4 channel or change its TLS guards.

`WAVEMESH_AGENT_PUBLIC_PROFILES_ENABLED` defaults to `false`. Enabling it requires
primary mTLS shadow mode and a compatible API/gateway. Deploy API/schema first,
then matching Builder/Agent, then opt in after acceptance. Old packages/API paths
can refuse the optional cycle without repeating the successful access command.

`placement_profiles.py` shares the existing Node mutation lock and private-file
helpers. It reads one completed placement fence and current durable identity,
local configuration, authenticated loopback panel client/inbounds and subLinks.
It checks scope and observes each source twice before exporting. An incomplete
fence, unsafe/missing state, concurrent mutation or observable drift refuses the
result. External UI writers are not fenced; this observation is not native engine
custody. The API independently checks complete accepted rights and material proof.

The existing native-subscription owner validates every UUID/public Entry host443/
published path and a complete bounded set. Supported VLESS/XHTTP/TLS/stream-one
parameters are preserved; unknown/ambiguous transports, nested destinations,
direct-RU routes and prepared channels refuse export. Native remarks are replaced
by configured public route names. The finding collector retains its narrower
read permissions; the profile collector adds only validated local subLinks GET.

Collection runs in a bounded child with stdout/stderr suppressed. Its only
credential-bearing output is a private temporary file, removed after delivery.
The ordinary health journal carries no URI/profile payload. Round-robin collection
also prioritizes the last completed placement command. mTLS POST uses the separate
public-profile endpoint, bound to the same command and Node configuration version.
An identical metadata retry is idempotent; conflicting content cannot overwrite a
receipt. Recollection after restart/lost delivery is read-only, never provisioning.

Installer/rollback includes the optional collector. Old backups can ignore its
absence. The Builder codec lives in its existing installed library; unavailable
or unsupported versions refuse collection. Rollback stops collection and retains
SaaS metadata/identity and the service derivation key. Already issued URLs still
need a compatible SaaS reader. No Node admission, live flag, key or service change
is authorized by this document.

Portable tests cover codec, privacy, auth/configuration gates and failure
isolation. Real Linux tests cover private files/flock/collection; actual PostgreSQL
tests cover receipt isolation, concurrency and read-only delivery. A Windows mock
or source type check does not replace those native checks or final hosted gates.
