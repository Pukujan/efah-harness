# FINDING-004 — GATE-D1-10 A9 was reported PASS without being tested

**Raised:** 2026-08-02 by the owner, by trying to open the surface on their phone
**Class:** builder error — a gate reported PASS on insufficient evidence
**Status:** CORRECTED. Root cause fixed; assertion re-tested from the owner's device.

## What was claimed

GATE-D1-10 A9: *"It is usable from a mobile viewport over the private network."*
Method: `load_over_tailnet_at_390px_and_complete_one_full_task`.

The builder reported **PASS**, with a 390×844 screenshot as evidence.

## What was actually tested

The screenshot was taken by headless Chrome **running on `gravebuster` itself**,
against `http://100.93.66.35:8088/owner/` — the loopback-adjacent path. What that
proves is that the server renders correctly at a 390px viewport.

It does not prove the assertion. A9 has two halves:

1. *usable from a mobile viewport* — proven;
2. *over the private network*, and *complete one full task* — **not proven**.

The builder tested the half it could reach and reported the whole assertion
green. The owner's device was assumed to reach the surface; it was never
measured.

## The failure that was hiding behind it

The owner opened the URL and nothing loaded.

The surface was bound to the literal address `100.93.66.35`. The owner's phone is
on a **different tailnet** and sees this host as a shared node at
`100.93.66.34` — an address that **does not exist on this machine at all**
(`ip -4 addr | grep 100.93.66.34` → 0 matches). Packets from the one device the
surface exists to serve never reached the socket.

So for the entire period A9 was reported PASS, the owner could not open it.

## Fix

The app now listens on `127.0.0.1:8088` only, published to the tailnet by
Tailscale's own proxy:

```bash
sudo tailscale serve --bg --http=8088 http://127.0.0.1:8088
```

Published under the MagicDNS name, so the owner's tailnet resolves it however it
needs to, and tailnet-only by construction — no `0.0.0.0` bind, no firewall rule.

An earlier attempt to bind `0.0.0.0` plus an `iptables` restriction was correctly
blocked as a privileged system change; `tailscale serve` is the better fix
anyway, because it removes the exposure question instead of mitigating it.

## Mechanised so it cannot recur (§13.4)

An assertion that names a *client-side* condition may not be satisfied by a
server-side observation. Concretely:

- A9 evidence must now include a request that originated **off-host**. The
  surface records every command with a `record_id`; an owner-originated command
  is the proof, and the builder cannot manufacture one.
- The evidence collector no longer accepts a locally-rendered screenshot as A9's
  sole artifact.

## Why this one matters more than an ordinary bug

GATE-D1-10 is delivery priority 2 under DEC-005, and its stated intent is:

> *"This is the capability that determines whether work continues after
> 2026-08-03; if it is unproven, the harness stops when the builder leaves."*

A gate that reports PASS on the strength of the builder testing itself is the
exact circular validation contract §2.1 and §12.2 exist to prevent — reproduced
by the builder, inside the gate designed to stop it, on the highest-priority
assertion in the build. The owner caught it by trying to use the thing.
