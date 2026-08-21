# owui_meta — Open WebUI Meta-Tool

A server-side Open WebUI tool that lets the model query the platform's **own internal API** to answer questions about the user's data: chats, files, prompts, tools, models, knowledge.

**Status:** in development — see [PLAN.md](./PLAN.md) for the implementation roadmap and [DESIGN.md](./DESIGN.md) for the full design.

## What makes it different

**No credentials to configure.** The tool authenticates with the token of the user making the request, extracted by Open WebUI's `AuthTokenMiddleware` into `request.state.token` (Bearer header, session cookie, or API key). No service keys, no hardcoded secrets — the tool runs inside the user's request context and reuses the platform's entire authorization chain.

## Security model (by design)

- **Endpoint allowlist** with typed methods — no arbitrary URL calls, no SSRF.
- **Read-only except one explicit write**: `delete_files(file_ids)` (user-authorized batch deletion of specific files) is the only write operation; everything else is query-only. The backend re-verifies authorization and removes each file from storage, metadata and the vector index.
- **`Content-Type` validation** — the SPA HTML catch-all returns HTTP 200, so only JSON (or explicitly allowed binary types) is trusted.
- **Mapped errors** — 401/403/404/5xx become readable messages; 404 never reveals whether a resource exists.
- **Truncation** — responses are capped before reaching the model context.
- **No token logging** — the credential never appears in logs or tool output. (`GET /api/v1/auths/` echoes the request token in its body; the tool field-whitelists the profile so the token never reaches the model, in any output format.)
- **Every response is field-whitelisted** — no method serializes a raw API body. Detail methods (profile, chat, skill) pick explicit fields; list methods summarize. Secret-bearing GETs (`auths/api_key`, tool valves, tool source, external DB configs, admin configs) are **not** in the allowlist.
- **Output-boundary guards** — even if a future method (or a future server field) leaked something, two guards stop it at the output boundary: `_sanitize` drops any credential-named key with a string value (boolean permission flags like `api_keys` are kept), and `_run` redacts the raw token string from success and error output. A static tripwire test pins that no raw server body reaches `_ok`.

## Output format

The tool returns **Markdown by default** (`output_format` valve, default `markdown`):

- Lists → **tables** with a summary line (`**Files: 2 (104 total on server)**`), **IDs always present** so the model can call follow-up methods.
- Raw numeric values passed through unformatted — byte sizes as `8796`, never `8.8 KB`; readable UTC dates.
- Profile → flat bullets; chat → heading + per-message blocks; file text → 100-char snippet in a fenced block; binary → metadata note; errors → plain-text `Error: …`.
- **`get_file_content` shows the file in the conversation** — images are **embedded inline in the message** (HTML `embeds` mechanism, styled like a snippet — not markdown, not artifacts) and enriched with **resolution and color depth** (width×height, Pillow mode, bits per channel — via Pillow, already bundled with Open WebUI); text and other binaries appear as an attachment with a clean 100-char snippet / note in the text.
- **Progress status events** (`verbose` valve, admin + per-user, default on) — the UI shows "Querying your chats…"-style progress while a method runs. **Errors** are shown as a message error block, always (even with `verbose` off), and consolidated to one per call — a batch delete with several failures shows a single "N of M files could not be deleted" instead of one toast per file.
- **`output_format`** — per-user valve, configurable from the chat session (dropdown Markdown/JSON, default Markdown). There is **no admin valve** for the format: each user chooses the format they prefer for their own chats. The tool's built-in default is Markdown.
- Set it to `json` for models that prefer structured objects.

Example of what the model receives for `get_chats()`:

```markdown
**Chats: 2**

| Title | Updated | ID |
|---|---|---|
| Budget planning | 2026-07-31 00:33 | b5d844f0-85c5-4cdc-8cf3-4f2366bc249e |
```

## Structure

```
owui_meta/
├── owui_meta.py        # The tool (class Tools) — install from Workspace → Tools
├── DESIGN.md           # Design document (validated against a v0.10.2 instance)
├── PLAN.md             # Iterative implementation plan
├── test/               # pytest suite (mocked backend; live suite env-gated)
├── pytest.ini
└── LICENSE
```

## Usage

Import `owui_meta.py` into Open WebUI at **Workspace → Tools → +** and attach it to a model. Then the model can call methods like `get_chats()`, `get_files()`, `search_chats("budget")`, `get_prompts()`, `get_skills()` — each answering with the requesting user's own data.

> **Method names (v0.21.0):** the `_my_` prefix was dropped from every method name (the tool only ever sees the requesting user's data) and the four chat list methods were unified into `get_chats(scope=…)`. Stored chat history referencing the old names (`get_my_chats`, `get_my_files`, …) shows those calls as unresolved; the model picks up the new names from the tool docstrings.

List methods support **pagination, sorting and filtering** (client-side, since the API does not expose them):

- `get_files(limit=50, sort_by="size" | "created_at" | "filename", sort_order="asc" | "desc", content_type="image/*", min_size=100000, max_size=1000000, filename="report")` — size in raw bytes.
- `get_chats(scope="all" | "pinned" | "shared" | "archived", limit=10, sort_by="updated_at" | "created_at", sort_order="asc" | "desc", tag="tool")` — `scope` selects the collection (default `"all"`); `"all"` includes folder + pinned chats (the backend hides them from the default listing); `tag` filters the list to chats carrying that tag (pure server-side filter, not a text search) and is accepted **only** with `scope="all"`.

Chat organization (Iteration 8) is covered by dedicated methods:

- `get_tags()` — the tag catalog (`name` + `id`), to answer “which tags do you use?”.
- `get_chats(scope="archived")` — archived chats.
- `get_chat_stats(chat_id)` — usage stats for one chat (message counts, models, tags, averages) from the **EXPERIMENTAL** `stats/usage` route; failure of that route is a clean error, never a crash.
- `get_folders()` — folders (`name`, `parent`, `expanded`, dates); a 403 on instances where folders are disabled maps to a readable error.
- `search_chats(text)` accepts the UI filter prefixes server-side: `tag:name`, `folder:name`, `pinned:true/false`, `archived:true/false`, `shared:true/false`, `tag:none` — and surfaces the per-result `snippet`.

Chat detail comes in two flavors:

- `get_chat_metadata(chat_id)` — organization metadata only (message count, models, tags, folder, pinned/archived, dates); **no message content in any format**.
- `get_chat_summary(chat_id)` — the same metadata plus a markdown snippet of the first and last 3 messages of the main branch (ellipsis for the middle); never the full conversation.

The tool iterates pages transparently (bounded by `MAX_PAGES`) and returns a **summarized** result: top N items + counts, never a full dump.

## File cleanup

Open WebUI keeps chat-attached files when the chat is deleted (verified in the v0.10.2 source — `delete_chat_by_id` never touches `Files`), so files from deleted chats stay in the library and storage forever. `delete_files` addresses the cleanup:

- `delete_files(file_ids)` — permanently deletes the given files in one pass (up to 50 per call). The whole list is validated before anything runs; per file it reports the name and removes it (the backend re-verifies you own it or have write access, and removes it from storage, metadata, KB associations and the vector index). A file that fails (missing / not yours / backend error) is reported by id without aborting the rest. **Irreversible.**

Finding the obsolete files is up to the model, using the existing read methods: `get_files()` exposes each file's `origin_chat_id`, and `get_chats()` gives the live chat ids — files whose origin chat is not in that list are cleanup candidates. Typical flow: `get_files()` + `get_chats()` → identify the orphans → `delete_files([ids...])` after user confirmation.

## Requirements

Installed automatically by Open WebUI on first load:

- `httpx` — async HTTP client for internal API calls

## License

MIT — see [LICENSE](./LICENSE).
