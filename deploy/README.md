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
http://100.93.66.35:8088/owner/
```

## Why it binds to the tailnet address and not 0.0.0.0

Reachable from the owner's phone on the private network, and from nowhere else.
`environments.yaml` makes the same choice for the protected TerminusDB instance
and for this project's Phoenix and OTel collector: loopback or tailnet, never a
public bind.

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
