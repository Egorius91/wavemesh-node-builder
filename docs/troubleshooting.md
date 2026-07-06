# Troubleshooting

## Let's Encrypt fails

Most common reason: provider firewall blocks TCP 80.

Check:

```bash
curl -I http://your-domain.com
sudo ufw status
```

Open in provider panel:

- TCP 80
- TCP 443

Then retry:

```bash
wavemesh repair --ssl
```

## Subscription exposes local port

Run:

```bash
wavemesh validate-subscription
```

If it fails, regenerate subscription from canonical config:

```bash
sudo bash install.sh --domain your-domain.com --email admin@example.com
```

Future versions will include `wavemesh repair --subscriptions` regeneration.

## nginx config fails

Run:

```bash
sudo nginx -t
sudo journalctl -u nginx -n 100 --no-pager
```

## Correct public ports

Only these should be reachable externally:

- 80/tcp
- 443/tcp

Xray local port and panel port should not be public.
