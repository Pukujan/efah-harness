# Deploying the owner control surface

Contract **EFAH-CONTRACT-001 v1.1 §11.7**. This is the capability that decides
whether work continues after 2026-08-03, so it is deployed as a supervised
service rather than left to a shell that dies with its terminal.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/efah-owner-surface.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now efah-owner-surface
loginctl enable-linger "$USER"       # survives logout
```

Then, from a phone on the tailnet:

```
http://gravebuster.tail733a0f.ts.net:8088/owner/
```

## Why it binds to localhost behind `tailscale serve`

The app listens on `127.0.0.1:8088` only. Tailscale's own proxy publishes it to
the tailnet:

```bash
sudo tailscale serve --bg --http=8088 http://127.0.0.1:8088
tailscale serve status        # should show "(tailnet only)"
```

**This replaced binding directly to the tailnet IP, which did not work.** The
owner's phone is on a *different* tailnet and sees this host as a shared node
under a different numeric address than the one the host holds locally
(`100.93.66.34` on the phone vs `100.93.66.35` here — and `.34` does not exist
on this machine at all). A socket bound to the literal local address therefore
never received the phone's packets, and the surface was unreachable from the one
device it exists to serve.

`tailscale serve` fixes this properly: it publishes under the MagicDNS name, so
the owner's tailnet resolves it however it needs to, and it is tailnet-only by
construction — no `0.0.0.0` bind and no firewall rule.

**Use the hostname, not the IP.** The IP is the thing that was wrong.

## Checking it

```bash
systemctl --user status efah-owner-surface
curl -s http://100.93.66.35:8088/owner/health
journalctl --user -u efah-owner-surface -n 50
```

`health` returning `"vendor_neutral": true` with the unit's
`UnsetEnvironment=ANTHROPIC_*` in force is the live proof of GATE-D1-10 A1.

## Credentials

The unit reads `EnvironmentFile=-/home/yoav/.efah/surface.env` (optional, hence
the `-`). It holds `TERMINUSDB_ADMIN_PASS` and nothing else, mode `0600`, outside
the repository — the same reference-only discipline `secrets.refs.yaml` requires.

Without it the surface still starts and honestly reports
`project_state: FAILED_INFRASTRUCTURE` rather than showing numbers it did not
read. That is the intended behaviour, not a bug: a control surface that
fabricates state is worse than one that admits it cannot see.

**The protected identity credential (`TERMINUSDB_PROTECTED_PASS`) is deliberately
absent.** The surface has no route to `:6364`; only the owner audit path does
(contract §11.2, `environments.yaml → terminusdb_protected.withheld_from`).
