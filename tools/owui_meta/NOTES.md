# NOTES — Design inconsistencies & open decisions (2026-08-21)

Living notes for the Iteration 9 rework. Each entry documents a real
inconsistency found when contrasting the design (DESIGN.md / PLAN.md) with
the implementation (`owui_meta.py`) and the v0.10.2 backend. These are the
"flecos" that surfaced during review of the `get_chats(scope=…)` /
`_my_`-drop rework (tasks 9.9 / 9.10) — cheap to resolve on paper now,
expensive to discover mid-implementation.

---

## N1. `tag` × `scope != "all"` does not exist in the backend

**Status:** open decision — impacts behavior.

**Inconsistency:** PLAN §9.9 says *"`tag=` composes with every scope
(validated per route)"*. That is imprecise: there is no backend route that
combines a scope with a tag filter.

**Evidence (`owui_meta.py`):**

- `_get_my_chats(tag=…)` → `POST /api/v1/chats/tags` `{name, skip, limit}`
  (filters by tag + user, **no scope**).
- `_get_pinned_chats` / `_get_shared_chats` / `_get_archived_chats` → their
  own routes (`/chats/pinned`, `/chats/shared`, `/chats/archived`), **no tag
  parameter**.

So "pinned + tag" has no direct route.

**Options:**

- **A) Client-side intersection** — fetch the scope list + fetch the tag set
  (`POST /chats/tags`), cross ids. Works for every scope; +1 internal call
  per query; honors the tool's existing "transparent iteration" style.
- **B) Restrict `tag` to `scope="all"`** — clean `ToolError` otherwise
  ("tag filter only applies to scope='all'"). Zero new requests, simplest
  contract, but the model must know the restriction.

**Recommendation:** B for v1 of `get_chats` (simplest, predictable); A as a
follow-up if the model actually needs scoped+tagged listings.

---

## N2. Pagination is asymmetric across scopes

**Status:** document (no code change planned).

**Inconsistency:** a single `limit` parameter behaves differently depending
on `scope`:

| Scope | Backend | `limit` semantics |
|---|---|---|
| `all` | `GET /api/v1/chats/` | iterates pages of 50 (`MAX_PAGES`), then slices to `limit` |
| `pinned` / `shared` | `/chats/pinned`, `/chats/shared` | accept `pageSize`; sliced to `limit` |
| `archived` | `/chats/archived` | **no server-side pagination** (whole list returned); sliced to `limit` |

**Action:** fix the `get_chats` docstring/spec to state per-scope `limit`
semantics ("top-N after iteration", "top-N of the returned list"), so the
behavior is documented rather than discovered.

---

## N3. Residual names after the `_my_` drop (9.10)

**Status:** tidy-up for the 9.10 commit.

**Inconsistencies:**

1. **Public method** `get_knowledge_bases` keeps an asymmetric name after
   everything else loses `_my_` — consider `get_knowledge` for symmetry
   (or keep, if "bases" reads better to the model; decide).
2. **Private methods** `_get_my_profile`, `_get_my_chats`, `_get_my_files`,
   `_get_my_prompts`, `_get_my_tools`, `_get_my_skills`, `_get_my_folders`,
   `_get_my_tags` would be left with `_my_` while the public names drop it —
   rename the privates in the **same commit** so no `my` residue remains in
   the codebase.
3. `get_file_content` is the deliberate exception (it is content, not a
   "my" list) — no change; confirm it stays.
4. `get_models` is already `_my_`-free — no change.

**Action:** fold into task 9.10; update `test_docstrings.py`-adjacent
references (names must match signatures).

---

## N4 (context). Iteration 9 status snapshot (2026-08-21)

For orientation when reading N1–N3:

- **DONE (v0.17.0–v0.20.0):** 9.1 `tag:` scope-limiter verified+pinned ·
  9.2 image metadata via Pillow · 9.3 chat-stats recompute (backend bug) ·
  9.4 fail-loud sanitizer + allowlist tripwire.
- **Pending:** 9.5 `delete_files` destructive test (optional, sandbox) ·
  9.7 `folder:` name-resolution fix · 9.8 `search_chats` requires a text
  term · 9.9 `get_chats(scope=…)` unification · 9.10 `_my_` drop.
- **DEFERRED:** 9.6 chat date-range filter (applies to `get_chats(scope="all")`).
