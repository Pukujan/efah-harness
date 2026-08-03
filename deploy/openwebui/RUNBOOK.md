# Open WebUI — how this container is created, and how to harden it

**Written 2026-08-03.** Until this file existed there was **no declarative source
for the `efah-openwebui` container** — no compose file, no systemd unit, no
`com.docker.compose.*` labels. It was created by a bare `docker run` whose
arguments lived only in someone's shell history.

That is not a documentation gap, it is the cause of two live defects. The data
volume (`21:07:24`) is **older than the container** (`21:45:36`), so an
undocumented recreate has already happened once — and it silently discarded an
admin setting (§R2) and destroyed a signing key (§R1). This file is the fix.

---

## 1. Observed state, 2026-08-03

| | |
|---|---|
| Container | `efah-openwebui`, `d16a9c638181`, created 2026-08-02T21:45:36Z, running/healthy |
| Image tag | `ghcr.io/open-webui/open-webui:main` — **rolling; not a pin** |
| **Digest** | `sha256:6a773e5c3a246b65cbe74ce942b294292c0e5f81c138f703d111bc162f7d7c3d` |
| Probed version | **0.11.0** (`GET /api/config → .version`) |
| Build revision | `01f4282f1ffe0d6212f58d3afbeae21fffd0c4be` |
| Network | `host` — with `HOST=127.0.0.1`, so the listener is loopback-only on **8095** |
| Volume | `efah-openwebui:/app/backend/data` (720 KB `webui.db`, 4.1 MB WAL, 320 Chroma collections, 62 MB) |
| Restart | `unless-stopped` · Runtime `runc` · User `0:0` |

**Seven operator-set env vars.** Everything else in the container env is baked
into the image and must **not** be re-passed — re-passing pins you to today's
image defaults across future digest bumps.

```
HOST=127.0.0.1   PORT=8095   ENABLE_PERSISTENT_CONFIG=False
OPENAI_API_BASE_URL=http://127.0.0.1:8088/v1   OPENAI_API_KEY=<redacted>
ENABLE_OLLAMA_API=False   WEBUI_NAME=EFAH
```

---

## 2. Why env vars are authoritative — measured, not read from docs

`ENABLE_PERSISTENT_CONFIG=False` makes `Config.get` short-circuit to
`DEFAULT_CONFIG` and **never consult `webui.db`**. Confirmed in the running
image's own source, then confirmed by a natural experiment already present on
this system:

- `webui.db` row: `ui.enable_signup = false`, set 2026-08-02 21:43:02
- `ENABLE_SIGNUP` env default: `True`, and the var is unset
- Live `GET /api/config`: **`enable_signup: true`**

The DB says false, the env default says true, the process reports true. **The DB
is inert.** No admin-API approach is needed, and no recipe written against Open
WebUI's older single-blob config schema applies — this build uses a per-key
`config` table.

**One caveat:** a logged-in admin can still flip these toggles at runtime. The
change is real for the process lifetime and evaporates on restart. Env vars are
*boot*-authoritative, not admin-proof.

---

## 3. The four contract violations this closes

`BUILD-VS-INTEGRATE-001` requires Open WebUI's own RAG, tools and model
management to stay off. Currently on:

| Feature | Env var | Why it must be off |
|---|---|---|
| `code_execution` | `ENABLE_CODE_EXECUTION` | Pyodide in the browser — **the only general-purpose code execution in the deployed system**, outside every gate |
| `code_interpreter` | `ENABLE_CODE_INTERPRETER` | same |
| `memories` | `ENABLE_MEMORIES` | violates `model-policy.yaml → session_policy: chat_transcript_as_project_memory: forbidden` |
| `evaluation.arena` | `ENABLE_EVALUATION_ARENA_MODELS` | injects models the harness did not route |

---

## 4. Backup — run this first

`webui.db` is 720 KB but its WAL is **4.1 MB and newer**. A plain `cp` captures a
stale, truncated database. Use the SQLite backup API, which is consistent against
a live writer.

```bash
mkdir -p /home/yoav/efah/efah-harness/.data/backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

docker exec efah-openwebui python -c "
import sqlite3
src = sqlite3.connect('file:/app/backend/data/webui.db?mode=ro', uri=True)
dst = sqlite3.connect('/tmp/webui.backup.db')
src.backup(dst); dst.close(); src.close(); print('ok')"
docker cp "efah-openwebui:/tmp/webui.backup.db" \
  "/home/yoav/efah/efah-harness/.data/backups/webui-${STAMP}.db"

docker exec efah-openwebui cat /app/backend/.webui_secret_key \
  > "/home/yoav/efah/efah-harness/.data/backups/webui_secret_key-${STAMP}"
chmod 600 "/home/yoav/efah/efah-harness/.data/backups/webui_secret_key-${STAMP}"
```

Confirm `.data/backups/` is gitignored before writing a signing key into it.

---

## 5. Recreate — hardened, pinned by digest

**Step 0 is mandatory, not optional.** See §R1.

```bash
# 0 — capture the signing key BEFORE removing the container.
#     It lives in the writable layer, NOT the volume. Losing it invalidates
#     every JWT (4-week expiry) and logs the owner out.
OWUI_SECRET="$(docker exec efah-openwebui cat /app/backend/.webui_secret_key)"
[ -n "$OWUI_SECRET" ] || { echo "REFUSING: secret key empty"; exit 1; }

# 1 — stop and remove.  NO -v.  Never `docker rm -v`.  Never `docker volume rm`.
docker stop efah-openwebui
docker rm  efah-openwebui

# 2 — recreate
docker run -d \
  --name efah-openwebui \
  --network host \
  --restart unless-stopped \
  --log-driver json-file --log-opt max-size=100m --log-opt max-file=3 \
  -v efah-openwebui:/app/backend/data \
  -e HOST=127.0.0.1 \
  -e PORT=8095 \
  -e ENABLE_PERSISTENT_CONFIG=False \
  -e OPENAI_API_BASE_URL=http://127.0.0.1:8088/v1 \
  -e OPENAI_API_KEY=efah-local \
  -e ENABLE_OLLAMA_API=False \
  -e WEBUI_NAME=EFAH \
  -e WEBUI_SECRET_KEY="$OWUI_SECRET" \
  -e ENABLE_CODE_EXECUTION=False \
  -e ENABLE_CODE_INTERPRETER=False \
  -e ENABLE_MEMORIES=False \
  -e ENABLE_MEMORY_SYSTEM_CONTEXT=False \
  -e ENABLE_EVALUATION_ARENA_MODELS=False \
  ghcr.io/open-webui/open-webui@sha256:6a773e5c3a246b65cbe74ce942b294292c0e5f81c138f703d111bc162f7d7c3d
```

Deliberately **not** passed: `--user`, `--workdir`, the entrypoint, the
healthcheck, and the 25 image-baked env vars — all verified identical to image
defaults. `-p` is illegal under `--network host`. `ENABLE_SIGNUP=False` is
omitted on purpose; see §R2.

**Better end state for a later change:** `-e
WEBUI_SECRET_KEY_FILE=/app/backend/data/.webui_secret_key` puts the key in the
volume, so no future recreate can lose it, and keeps the secret out of
`docker inspect` and shell history.

---

## 6. Verify

```bash
docker inspect efah-openwebui --format \
  '{{.State.Status}} {{.State.Health.Status}} {{.Image}} {{.HostConfig.RestartPolicy.Name}}'
#   running healthy sha256:6a773e5c…7c3d unless-stopped     (allow 30-60s for health)

curl -s http://127.0.0.1:8095/api/config | jq '.version, .features.auth'
#   "0.11.0"  true
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8095/api/models
#   401

docker exec efah-openwebui printenv | grep -E \
  'ENABLE_(CODE_EXECUTION|CODE_INTERPRETER|MEMORIES|MEMORY_SYSTEM_CONTEXT|EVALUATION_ARENA_MODELS|PERSISTENT_CONFIG)'
#   six lines, all False

docker exec efah-openwebui sh -c 'ls /app/backend/data/webui.db && ls /app/backend/data/vector_db | wc -l'
#   the db, and 320   -- proves the volume survived
```

Then one real chat turn through the UI, and confirm the five `efah-*` modes are
still the only models listed with no raw vendor name among them.

**The DB rows will still read `true` after this. That is expected** — they are
inert under `ENABLE_PERSISTENT_CONFIG=False`. Verify from `printenv` and the live
API, never from `webui.db`.

---

## 7. Rollback

Same command with `ghcr.io/open-webui/open-webui:main` and without the five
hardening lines — but keep `-e WEBUI_SECRET_KEY="$(cat …/webui_secret_key-<STAMP>)"`
so the rollback preserves sessions. Note `:main` is rolling: for a genuine code
rollback substitute the digest, since the env is what you are actually reverting.

If `webui.db` itself needs restoring, stop the container and remove the stale
`-wal`/`-shm` beside the restored file, or it will corrupt.

---

## Risks

**R1 — CRITICAL. The signing key is in the container layer.** 33 bytes at
`/app/backend/.webui_secret_key`, absent from the volume, mtime = this
container's start (so the previous one's key was already lost). `docker rm`
destroys it and `start.sh` generates a new one. Step 0 above is the mitigation.

**R2 — CRITICAL AND LIVE NOW. Signup is open.** `/api/config` reports
`enable_signup: true` despite the admin disabling it, because the setting was
discarded on the last recreate. Anyone reaching `127.0.0.1:8095` can create an
account. The fix is `-e ENABLE_SIGNUP=False` — **but it breaks
`tests/integration/test_openwebui_e2e.py`**, whose fixture signs up a probe
account. Sequence it: pre-create the probe user or change the fixture, *then*
disable signup. Do not bundle it silently.

**R3 — This change set does NOT clear BUILD-VS-INTEGRATE-001.** RAG is still on
and has actively run: 320 Chroma collections, 62 MB, 32 uploads. Turning it off
(`BYPASS_EMBEDDING_AND_RETRIEVAL=True`) breaks three passing tests that assert
RAG works. **The contract and the test suite currently contradict each other.**
That is an owner decision. Do not record this change as "debt cleared."

**R4 — A provenance hole, not just a cost leak.** `ENABLE_TITLE_GENERATION`,
`ENABLE_TAGS_GENERATION`, `ENABLE_FOLLOW_UP_GENERATION` and
`ENABLE_RETRIEVAL_QUERY_GENERATION` all default `True`, and `task.model.default`
is `""` — so they run **against the `efah-*` mode itself**. Measured
2026-08-03: `.data/owner_surface_ledger.jsonl` holds 61 rows
(`owner_command` 49, `blocker` 6, `owner_instruction_result` 4, `work_unit` 2)
and **zero chat turns**. So these calls reach the gateway with no lease, no
blinded alias and no provenance envelope. Also default-on and unreviewed:
`ENABLE_AUTOMATIONS`, `ENABLE_NOTES`, `ENABLE_ADMIN_EXPORT`,
`ENABLE_ADMIN_CHAT_ACCESS`, `ENABLE_COMMUNITY_SHARING`.

**R5 — A leftover admin account.** `dbg / dbg@efah.local` has `role=admin`
alongside the owner and survives the recreate. With R2 open, delete or demote it.

**R6 — Nothing else is lost.** `docker diff` shows the writable layer holds only
`__pycache__`, a torch temp stub, and the secret key. R1 is the complete list.

**R8 — The hardening depends on one variable staying set.** The 341 `config`
rows still say `true` for all four features. If `ENABLE_PERSISTENT_CONFIG` ever
flips to `True` — and the *env default is* `True`, so omitting it is enough —
**all four violations return instantly and silently.** Consider also correcting
those four DB rows to `false`, so no single flag can reintroduce them.
