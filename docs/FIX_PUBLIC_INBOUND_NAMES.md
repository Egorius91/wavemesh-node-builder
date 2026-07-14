# Public inbound names

WaveMesh keeps the stable technical routing identifier in the 3X-UI/Xray `tag` field (`wm-route-*`) and writes the user-facing route name to the 3X-UI `remark` field.

This allows clients which prefer the inbound remark over the VLESS URI fragment to display names such as:

- `RU -> Germany`
- `RU -> Frankfurt`
- `⚡ RU -> Auto Europe`

The change does not modify route tags, UUIDs, paths, outbounds, balancers, subscriptions, or routing behavior.

## Apply on an existing Entry node

After installing the updated CLI and libraries, reconcile the desired state so existing 3X-UI inbounds receive the new remarks:

```bash
sudo wavemesh reconcile --dry-run
sudo wavemesh reconcile --apply
sudo wavemesh cascade auto health --json
sudo wavemesh cascade health
sudo wavemesh subscription validate
```

Then refresh the subscription in the client. Some clients cache profile titles; remove and import the subscription again only if a normal refresh does not update them.
