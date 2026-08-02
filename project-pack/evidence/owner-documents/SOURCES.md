# Source documents

Owner-measured source material for `model-policy.yaml` and
`DEC-002-eval-gateway-for-gate-bearing-roles.md`.

Copied into the pack 2026-08-02 from the owner workstation, `Desktop/litellm-proxy/`.
The measurements they record were taken **2026-08-01**.

## Files and hashes

| Pack filename | Source filename | sha256 |
|---|---|---|
| `MODELS.md` | `MODELS.md` | `8fe2e925955207d237e84c6ce998a750cd4ea590235f801aaae4b94609623ebc` |
| `EVALLAB.md` | `EVAL-LAB.md` | `544eef6f10bf0bf0976cae97ab494bfa683c7fbf93ead8d74811b317b05acba6` |
| `CONFIGURATIONGUIDE.md` | `CONFIGURATION-GUIDE.md` | `48de33ee62bc175c32baccc70e45b9977e28021694debf60e385ed6490407c0d` |

Hyphens were removed from two filenames on copy so that the citation already
present at `model-policy.yaml` lines 4-5 resolves inside the pack. Contents are
byte-identical to the source files; the hashes above are of that identical
content and were verified to match on both machines after transfer.

## What derives from what

`model-policy.yaml` states at lines 4-6 that it was populated 2026-08-01 from
this inventory and that every latency, price, and tool-reliability figure in it
is measured rather than assumed. Specifically:

- The `measured:` blocks (`median_latency_s`, `worst_s`, `price`, `tools`)
  derive from `MODELS.md` section 1.6 (latency, where variance is called out as
  mattering more than the median), sections 1.1-1.2 (cost, including the finding
  that billing follows the channel group rather than list price), and section 2
  (tool-calling reliability, three attempts per model).
- `prohibited_models` derives from the same cost findings in `MODELS.md`
  sections 1.1-1.2.
- Model tiering derives from `EVALLAB.md` Tiers A/B/C and its
  "Excluded, with reasons" section.
- The `DEC-002` gateway split derives from `CONFIGURATIONGUIDE.md`
  "The two gateways", together with its Trap 3 (`drop_params` silently
  rewriting requests) and Trap 4 (pooling as a hidden retry), which are the
  stated reasons production must not be used to collect evaluation evidence.

## Not found

`EVALENDPOINT.md` is cited at `model-policy.yaml` line 5 but **does not exist**.
A search of the owner workstation on 2026-08-02 -- Desktop, `litellm-proxy`,
`litellm-eval`, Downloads, and Documents, including any markdown file with
"endpoint" or "eval" in its name -- returned no such document, and none is
present on the build host.

It has deliberately **not** been reconstructed or invented. Two consequences:

1. The citation at `model-policy.yaml` line 5 is unresolved and should either be
   corrected to the documents that do exist or satisfied by supplying the real
   file.
2. The DB-less justification in `DEC-002` cites the in-pack sources
   (`DEC-002`'s own "The eval service must also remain DB-less" paragraph and
   `environments.yaml` lines 85-88, `must_remain_dbless: true`) rather than
   `EVALENDPOINT.md`, and remains so until that document is supplied.
