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
- **No token logging** — the credential never appears in logs or tool output.

## Output format

The tool returns **Markdown by default** (`output_format` valve, default `markdown`):

- Lists → **tables** with a summary line (`**Files: 2 (104 total on server)**`), **IDs always present** so the model can call follow-up methods.
- Raw numeric values passed through unformatted — byte sizes as `8796`, never `8.8 KB`; readable UTC dates.
- Profile → flat bullets; chat → heading + per-message blocks; file text → fenced block; binary → metadata note; errors → plain-text `Error: …`.
- Set the valve to `json` for models that prefer structured objects.

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

Import `owui_meta.py` into Open WebUI at **Workspace → Tools → +** and attach it to a model. Then the model can call methods like `get_my_chats()`, `get_my_files()`, `search_chats("budget")`, `get_my_prompts()` — each answering with the requesting user's own data.

## Requirements

Installed automatically by Open WebUI on first load:

- `httpx` — async HTTP client for internal API calls

## License

MIT — see [LICENSE](./LICENSE).
