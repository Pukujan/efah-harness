# BUILD_VS_INTEGRATE-001 — owner chat front end

**Required by:** contract §14.2 — *"Before custom implementation, the agent MUST
produce a `BUILD_VS_INTEGRATE` record"*
**Raised:** 2026-08-02 · **Status:** RECORDED, awaiting owner selection
**Prompted by:** the owner, asking *"is there any dependency we can integrate
instead"* — before the builder had produced this record, which the contract
required first.

## Capability

A multi-session chat front end the owner can drive from a phone: conversation
threads with history, a way to select what kind of work a message is (plan,
research, build), and visibility of results and open blockers.

## Why this record exists at all

`dependency-policy.yaml` sets `integrate_by_default: true` and lists
`custom_project_management_ui` as **prohibited**, `reason:
duplicates_plane_and_delays_e2e`. §28 names *"UI polish that delays the real
end-to-end workflow"* as an explicit non-goal. The hand-written surface at
`/owner/` already sits close to that line; extending it into a chat application
would cross it.

## Existing candidates

All three were retrieved through Context7 and snapshotted with hashes (§16.1)
rather than recalled. The snapshots are the citation; the libraries are not.

### 1. Open WebUI — `/websites/openwebui`
Snapshot `C7-openwebui-openai-compatible-a61d143d`.

> "To ensure full compatibility with Open WebUI, servers should implement
> specific OpenAI-standard endpoints. The core chat functionality requires the
> /v1/chat/completions endpoint. Model discovery is facilitated by /v1/models,
> though models can be manually allowlisted if this endpoint is unavailable."

> "When configuring a connection, you must provide a Base URL and an API Key.
> … Optional fields include a display name, a Prefix ID for namespacing model
> IDs, and a specific list of models to use."

**Integration cost:** one endpoint. `/v1/models` is optional because the model
list can be allowlisted by hand, which matters here — the "models" are modes,
and a fixed list is more honest than a discovery endpoint pretending modes are
models.

### 2. LibreChat — `/websites/librechat_ai`
Snapshot `C7-librechat-custom-endpoints-9aefc459`.

> "LibreChat supports OpenAI API-compatible services as custom endpoints. …
> Configuration is done through `librechat.yaml`, with API keys stored in
> `.env`."

Config-as-code with an explicit `models.default` list and `fetch: false`, which
suits a fixed mode list. It documents LiteLLM as a custom endpoint directly —
useful as a warning rather than a recommendation, see the trap below.

### 3. LangGraph Agent Chat UI + local server — `/websites/langchain_oss_python_langgraph`
Snapshot `C7-langgraph-agent-chat-ui-087f6f72`. **Already the selected
`workflow_runtime`**, so §14.2 requires considering it first.

> "Agent Chat UI supports connections to both local and deployed agents.
> Configuration involves providing your graph name (Graph ID), the agent
> server's endpoint (Deployment URL), and optionally a LangSmith API key. After
> setup, the UI automatically fetches and displays interrupted threads from your
> agent."

The last sentence is the interesting one: **interrupted threads are exactly the
§10.7 owner-interrupt path**, which is how FINDING-005 reached the owner. A UI
that surfaces them natively is a close fit.

Against it: `langgraph dev` is documented as a *development* server, the chat UI
is a separate Node/pnpm application rather than a deployable service, and the
connection flow mentions a LangSmith API key. **Whether a self-hosted
production LangGraph Server needs a licence is not established by this snapshot
and the builder has not verified it** — recorded as an open question rather than
asserted either way, because §7.3 forbids a load-bearing claim without a source.

## The trap, recorded because it is the obvious shortcut

Both Open WebUI and LibreChat can be pointed **straight at LiteLLM** — LibreChat
documents that exact configuration. That takes minutes and produces a chatbot
talking to a model gateway with **no harness in the path**: no gates, no leases,
no provenance envelope, no citation validation, no blinded aliases. It would
look like success and would be the `free_form_llm_orchestrator` non-goal wearing
a chat interface.

The front end must therefore front **the harness**, not the gateway.

## Selected dependency

**Open WebUI**, with LibreChat as the recorded fallback.

| | |
|---|---|
| `capability` | owner-facing multi-session chat front end |
| `existing_candidates` | Open WebUI · LibreChat · LangGraph Agent Chat UI |
| `selected_dependency` | Open WebUI |
| `version` | **unpinned — must be probed and pinned at install** (`dependency-policy.yaml: pin_versions: true`) |
| `why_adapter_is_sufficient` | it requires only `POST /v1/chat/completions`; the harness already runs FastAPI, the selected `api` component |
| `custom_code_required` | an OpenAI-compatible façade over the existing LangGraph runtime: chat request → `thread_id` → graph invocation → streamed response. No UI code. |
| `rejected_reimplementation` | **true** — no chat client, no session store, no message history, no auth is written here |

Chosen over LibreChat on one measured property: model discovery is optional, so
the mode list can be allowlisted without the harness having to serve a
`/v1/models` endpoint that misrepresents modes as models. Both remain viable and
the façade is identical for either — the choice is reversible by configuration,
which is why it is not worth more deliberation than this.

Chosen over Agent Chat UI because that path adds a Node application and an
unresolved licensing question, to avoid writing one FastAPI route. The
LangGraph *runtime* is still what executes the work; only its UI is declined.

## What the harness must supply

- `POST /v1/chat/completions` — OpenAI shape, streaming, on the existing FastAPI app.
- **`model` carries the mode.** `efah-auto`, `efah-plan`, `efah-research`,
  `efah-build`. Dispatch is a deterministic table from mode to graph; no model
  decides which mode it is in, for the same reason `owner_surface.policy`
  classifies commands without a model.
- **A conversation is a LangGraph thread.** `src/workflows/runtime.py` already
  takes `thread_id` and resumes from the checkpointer. Multi-session needs no
  new dependency; it needs the consumer to stop bypassing the graphs.
- Every turn still enters the normal path: lease, blinded alias, provenance
  envelope, gate path. The façade adds a protocol, never an authority.

## Provenance of this record's own evidence

Every load-bearing claim here was checked by `research.claims.validate_claim` —
the §7.3 validator built earlier today — against the source it cites.
**8/8 SUPPORTED**, recorded in `evidence/BUILD-VS-INTEGRATE-001-claims.json`. A
negative control was run: a plausible, on-topic, fabricated quote attributed to
the same Open WebUI snapshot returned `UNSUPPORTED`, so the 8/8 means something.

The check also caught the builder. The three candidate retrievals were first
made through the **Claude Code Context7 MCP server**, which is a path
`environments.yaml` does not declare and which disappears when Claude Code
access ends. `verify_snapshot` refused them, correctly. Re-running the same
queries through the owner's declared Context7 credential returned **different
text** — two of the quotes were not in it. Rather than relabel the MCP
retrievals as `primary`, which would have been false, they were moved to
`evidence/context7-mcp-retrievals/` and the two claims that rest on them (C7,
C8) are cited at `SECONDARY_COMMENTARY` authority. C1–C6, which carry the
selection, cite the declared path or the contract.

## Honest debt

- **Open WebUI ships its own RAG, model management, and tools.** Those overlap
  the harness's own retrieval and routing and must stay switched off; two RAG
  systems with one UI is a provenance problem, not a feature.
- **Version is unpinned in this record.** It must be pinned at install with a
  probed version and an image digest per §16.3 before this decision is complete.
- **A second front end now exists.** `/owner/` remains the §11.7 control surface
  and does not go away — AMENDMENT-001 requires it, and it is what works when
  the chat client is down.
