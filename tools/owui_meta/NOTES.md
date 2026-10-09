# NOTES — Design inconsistencies & open decisions

**Merged into [PLAN.md](./PLAN.md) on 2026-08-21.** This file is kept as a
pointer only; the full content lives in the plan:

| Note | Where it now lives |
|---|---|
| **N1** — `tag` × `scope != "all"` does not exist in the backend | Task **9.9** → Design notes (decision taken: Option B — `tag` restricted to `scope="all"`) |
| **N2** — Pagination is asymmetric across scopes (`limit` semantics) | Task **9.9** → Design notes (document in the `get_chats` docstring) |
| **N3** — Residual names after the `_my_` drop (incl. `get_knowledge_bases`) | Task **9.10** → Design notes (renamed in the same commit; `get_knowledge_bases` **KEPT** — resolved 2026-08-21, matches Open WebUI's own “knowledge bases” nomenclature) |
| **N4** — Iteration 9 status snapshot (2026-08-21) | Header **Status** line of PLAN.md |

See the git history (`48b9f71`) for the original notes content.
