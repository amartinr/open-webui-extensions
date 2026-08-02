# owui_meta — Open WebUI Meta-Tool

A server-side Open WebUI tool that lets the model query the platform's **own internal API** to answer questions about the user's data: chats, files, prompts, tools, models, knowledge.

**Status:** in development — see [PLAN.md](./PLAN.md) for the implementation roadmap and [DESIGN.md](./DESIGN.md) for the full design.

## What makes it different

**No credentials to configure.** The tool authenticates with the token of the user making the request, extracted by Open WebUI's `AuthTokenMiddleware` into `request.state.token` (Bearer header, session cookie, or API key). No service keys, no hardcoded secrets — the tool runs inside the user's request context and reuses the platform's entire authorization chain.

## Security model (by design)

- **Endpoint allowlist** with typed methods — no arbitrary URL calls, no SSRF.
- **Read-only** in v1; admin methods gated by runtime role check.
- **`Content-Type` validation** — the SPA HTML catch-all returns HTTP 200, so only JSON (or explicitly allowed binary types) is trusted.
- **Mapped errors** — 401/403/404/5xx become readable messages; 404 never reveals whether a resource exists.
- **Truncation** — responses are capped before reaching the model context.
- **No token logging** — the credential never appears in logs or tool output. (`GET /api/v1/auths/` echoes the request token in its body; the tool field-whitelists the profile so the token never reaches the model, in any output format.)
- **Every response is field-whitelisted** — no method serializes a raw API body. Detail methods (profile, chat, skill) pick explicit fields; list methods summarize. Secret-bearing GETs (`auths/api_key`, tool valves, tool source, external DB configs, admin configs) are **not** in the allowlist.

## Output format

The tool returns **Markdown by default** (`output_format` valve, default `markdown`):

- Lists → **tables** with a summary line (`**Files: 2 (104 total on server)**`), **IDs always present** so the model can call follow-up methods.
- Raw numeric values passed through unformatted — byte sizes as `8796`, never `8.8 KB`; readable UTC dates.
- Profile → flat bullets; chat → heading + per-message blocks; file text → fenced block; binary → metadata note; errors → plain-text `Error: …`.
- **`output_format`** — per-user valve, configurable from the chat session (dropdown Markdown/JSON, default Markdown). There is **no admin valve** for the format: each user chooses the format they prefer for their own chats. The tool's built-in default is Markdown.
- Set it to `json` for models that prefer structured objects.

Example of what the model receives for `get_my_chats()`:

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

Import `owui_meta.py` into Open WebUI at **Workspace → Tools → +** and attach it to a model. Then the model can call methods like `get_my_chats()`, `get_my_files()`, `search_chats("budget")`, `get_my_prompts()`, `get_my_skills()` — each answering with the requesting user's own data.

List methods support **pagination, sorting and filtering** (client-side, since the API does not expose them):

- `get_my_files(limit=50, sort_by="size" | "created_at" | "filename", sort_order="asc" | "desc", content_type="image/*", min_size=100000, max_size=1000000, filename="report")` — size in raw bytes.
- `get_my_chats(limit=10, sort_by="updated_at" | "created_at", sort_order="asc" | "desc")`.

The tool iterates pages transparently (bounded by `MAX_PAGES`) and returns a **summarized** result: top N items + counts, never a full dump.

## Requirements

Installed automatically by Open WebUI on first load:

- `httpx` — async HTTP client for internal API calls

## License

MIT — see [LICENSE](./LICENSE).
