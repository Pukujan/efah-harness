# Context7 retrievals via the Claude Code MCP server

These three retrievals are **real evidence with weaker provenance than the pack's
snapshot cache**, and they live here rather than in
`project-pack/evidence/context7-snapshots/` for a reason worth stating.

`integrations.context7.verify_snapshot` requires `credential_alias` to be
`primary` or `secondary` — the two Context7 credentials `environments.yaml`
declares. These were retrieved through the **claude.ai Context7 MCP server**,
which is a third path the pack does not declare and which **disappears when
Claude Code access ends**. Labelling them `primary` to make the check pass would
have been false, and would have put three permanently-failing snapshots in the
pack's cache.

So the same queries were re-run through the owner's declared Context7 credential
over HTTPS, and *those* are the pack snapshots. They returned **different text**
— which is exactly why the citation validator matters: two quotes cited from
these retrievals are not present in the declared-path ones, and
`BUILD-VS-INTEGRATE-001` cites them from here, at
`SECONDARY_COMMENTARY` authority, rather than pretending they came from the
pinned path.

Claims C7 and C8 in `evidence/BUILD-VS-INTEGRATE-001-claims.json` rest on these.
Both are supporting detail, not the basis of the selection: C1–C6, which carry
the decision, all cite the declared path or the contract.
