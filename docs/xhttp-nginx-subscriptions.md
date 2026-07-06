# XHTTP, nginx and subscriptions

The main acceptance test for this project is not that nginx starts or that 3X-UI exists. The main acceptance test is that the public subscription is correct.

## Public subscription

```text
https://domain.com/sub/<random>/
```

## Expected links inside subscription

```text
vless://UUID@domain.com:443?type=xhttp&security=tls&path=%2Fapi%2Frandom%2F&host=domain.com&sni=domain.com&fp=randomized&encryption=none#Node-1
```

## Why fallback-generated subscriptions exist

3X-UI may generate links using an internal address, panel port, server IP, or local Xray port if external URL settings are not aligned. For MVP, the builder can generate a static subscription file from canonical node config. This makes validation deterministic.

Later iterations may replace fallback mode with 3X-UI subscription API if it can be configured reliably.
