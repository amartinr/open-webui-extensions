# Design Document — RAG Mode Controller

**Two-filter system for per-conversation RAG toggling.**

The Pipe integration described in section 6 is documented for architectural completeness but is **out of scope** for this project. It will be addressed separately in the `agent_loop_guard` project.

---

## Table of Contents

1. [Preface — Rationale](#preface--rationale)
2. [Purpose](#1-purpose)
3. [Architecture](#2-architecture)
4. [Filter A: rag_default_off — Full Files by Default](#3-filter-a-rag_default_off--full-files-by-default)
    - [Design rationale](#31-design-rationale)
    - [Responsibilities](#32-responsibilities)
    - [File reference persistence](#33-file-reference-persistence)
    - [Injection markers](#34-injection-markers)
    - [Knowledge Bases](#35-knowledge-bases)
5. [Filter B: rag_enable — Restore RAG on Demand](#4-filter-b-rag_enable--restore-rag-on-demand)
    - [Design rationale](#41-design-rationale)
    - [Responsibilities](#42-responsibilities)
6. [How the two filters interact](#5-how-the-two-filters-interact)
    - [Sequential execution via priority](#51-sequential-execution-via-priority)
    - [Example traces](#52-example-traces)
7. [Pipe: agent_loop_guard changes](#6-pipe-agent_loop_guard-changes)
    - [What the Pipe does today](#61-what-the-pipe-does-today)
    - [New responsibility](#62-new-responsibility)
    - [Tools affected](#63-tools-affected)
    - [Integration point](#64-integration-point)
8. [Filter ↔ Pipe coordination](#7-filter--pipe-coordination)
9. [Technical feasibility](#8-technical-feasibility)
10. [Installation and configuration](#9-installation-and-configuration)
11. [Edge Cases and Limitations](#10-edge-cases-and-limitations)
12. [Summary of design decisions](#11-summary-of-design-decisions)

---

## Preface — Rationale

Open WebUI ships with a powerful global RAG pipeline: when a user attaches files to a chat, the system chunks them, generates embeddings, and on every message performs semantic retrieval plus reranking to inject the most relevant fragments as context into the LLM prompt.

This default flow works well for general queries that need a synthesised answer drawn from multiple sources. It does not cover every use case, however. A lawyer reviewing contractual clauses, a doctor consulting a clinical protocol, or a researcher verifying a textual citation does not want reranked fragments — they want the complete document, intact, word for word, injected directly into context so the LLM can operate over it without information loss or reranking bias.

Open WebUI does not offer this capability natively. RAG is global and monolithic: semantic retrieval + reranking, always the same. There is no "full document" mode a user can toggle per task. The only workaround is the global env var `BYPASS_EMBEDDING_AND_RETRIEVAL`, which applies to every conversation of every user indiscriminately.

This document describes a system of **two Filters** that give the user full-document context by default while keeping standard RAG as an opt-in, plus a lightweight addition to an **existing Pipe** that removes semantic KB search tools when full-document mode is active.

---

## 1. Purpose

Let the user choose between two file-processing modes per conversation, with full-document context as the natural default:

| Mode | Behaviour | Use case |
|---|---|---|
| **`full_files`** (default) | Suppresses retrieval and reranking. Injects full file contents as a system message. Semantic KB search tools removed. | Legal review, detailed textual analysis, citation verification |
| **`rag`** (opt-in, via toggle) | Standard RAG: semantic retrieval + reranking. The built-in pipeline runs as normal. | General queries, synthesised multi-source answers |

The user switches to RAG mode by enabling a single toggleable filter. When the filter is off, full-document mode is the default. File references persist across server restarts.

---

## 2. Architecture

```
User → Open WebUI
               │
               ▼
    ┌──────────────────────────────┐
    │  FILTER A (always-on, prio 0) │
    │  rag_default_off              │
    │                               │
    │  — Always persists refs in    │
    │    chat.meta                   │
    │  — Always clears files[]      │
    │  — Injects full content if    │
    │    not already present         │
    │  — Always sets rag_mode       │
    │    = "full_files"             │
    └───────────┬──────────────────┘
                │
                ▼
    ┌──────────────────────────────┐
    │  FILTER B (toggle, prio 1)    │
    │  rag_enable                   │
    │                               │
    │  If ON:                       │
    │  — Restores files[] from      │
    │    chat.meta                   │
    │  — Removes rag_mode flag      │
    │  — Removes full content block │
    │  If OFF: passthrough           │
    └───────────┬──────────────────┘
                │
                ▼
       Middleware resolves NFC tools
       → chat_completion_files_handler()
       → get_builtin_tools()
       → body["tools"] with all tools
                │
                ▼
    ┌──────────────────────────────┐
    │  PIPE (existing manifold)     │
    │  agent_loop_guard             │
    │                               │
    │  — If rag_mode == "full_files":│
    │    removes query_knowledge_    │
    │    files, query_knowledge_     │
    │    bases from tools[]         │
    │  — If rag_mode absent:        │
    │    no-op                      │
    │  — All existing logic intact  │
    │  — Proxies to gateway         │
    └───────────┬──────────────────┘
                │
                ▼
          Gateway → LLM
```

---

## 3. Filter A: `rag_default_off` — Full Files by Default

**File:** `filters/rag_mode_selector/rag_default_off.py`

**Configuration:** always-on filter, `self.toggle = False`, `priority = 0`.

### 3.1 Design rationale

This filter represents the opinion that full-document context is the better default. It always runs and always enforces full-document mode. The user does not see it in the UI — it has no chip, no toggle. It is transparent infrastructure.

When this filter alone is active, every request with attached files receives the complete document content. The built-in RAG pipeline never runs because the files reference is always cleared before the handler sees it.

### 3.2 Responsibilities

Every time the inlet runs:

1. **Persist file references.**
   If `body["metadata"]["files"]` contains entries, extract their IDs and names and write them to `chat.meta["rag_mode_files"]`. This is the canonical source of truth for which files belong to this conversation. It outlives the current request, survives server restarts, and is the single place both filters consult for file references.

2. **Clear files from body.**
   Pop `files` from both `body["metadata"]` and `body`. This causes `chat_completion_files_handler()` at line 1779 of `middleware.py` to see no files and skip the entire retrieval + reranking step. Uses `.pop(key, None)` so the operation is safe even if a key is absent.

3. **Inject full content (if not already present).**
   Check `chat.meta["full_files_injected"]`. If a record exists **and** the injection marker `[injection:<id>]` is actually present in the system messages, the content was injected on a previous turn — skip injection. If either the record is absent or the marker is missing from messages (e.g. dropped by context compaction), retrieve the full content of each referenced file from the `file` table (via `open_webui.models.files.Files`, field `data.content`) and prepend a system message to `body["messages"]`.

   The dual check (record + message marker) ensures that if context compaction drops the injected block, the content is **re-injected** on the next turn even though the `chat.meta` record survives.

4. **Set the `rag_mode` flag.**
   Set `body["metadata"]["rag_mode"] = "full_files"`. This is the signal the Pipe reads to decide whether to filter semantic KB tools.

### 3.3 File reference persistence

`chat.meta["rag_mode_files"]` is stored in the `meta` JSON column on the `Chat` table (`models/chats.py` — `Column(JSON, server_default='{}')`). It survives server restarts.

Filter A writes to it whenever files are present in the body. Filter B reads from it when the user enables RAG mode. No other component writes to or reads from this key.

### 3.4 Injection markers

Filter A embeds a unique injection ID inside the content block so Filter B can locate
it without relying on magic strings:

```
[injection:a1b2c3d4-e5f6-...]
--- document.pdf ---
Full content of the first file...

--- notes.docx ---
Full content of the second file...
```

- `[injection:<uuid>]` is the presence check — Filter A scans system messages for this
  exact string (paired with the `chat.meta` record).
- Filter B reads the injection record from `chat.meta["full_files_injected"]` and scans
  messages for `[injection:<id>]` to locate and remove the entire block.
- The `chat.meta` record stores `{"id": "...", "at": <timestamp>, "files": [...]}`.

### 3.5 Knowledge Bases

Knowledge Bases are not affected by this filter. When the model has native function calling enabled, KBs are accessed through built-in tools, not through the RAG pipeline. This filter leaves KB-related data untouched. Removal of semantic KB tools is delegated to the Pipe.

---

## 4. Filter B: `rag_enable` — Restore RAG on Demand

**File:** `filters/rag_mode_selector/rag_enable.py`

**Configuration:** toggleable filter, `self.toggle = True`, `priority = 1`. Appears as a clickable chip in the chat UI.

### 4.1 Design rationale

This filter is the user-facing part of the system. When the user wants standard RAG behaviour (semantic retrieval + reranking), they toggle this filter on. When they want the full-document default, they toggle it off. The chip provides a single on/off affordance — no valves, no modals needed.

Because Filter B runs **after** Filter A (thanks to `priority = 1`), it receives `body` in whatever state Filter A left it. It can therefore reverse Filter A's decisions without either filter needing to know about the other.

### 4.2 Responsibilities

When **ON** (chip enabled, RAG mode active):

1. **Restore file references.**
   Read `chat.meta["rag_mode_files"]` and repopulate `body["metadata"]["files"]` with the persisted references. This gives `chat_completion_files_handler()` files to process, reactivating the standard RAG pipeline.

2. **Remove the `rag_mode` flag.**
   Delete `body["metadata"]["rag_mode"]` (via `.pop("rag_mode", None)`). The Pipe only removes semantic KB tools when this key has the value `"full_files"`. Removing the key signals "RAG mode" to the Pipe.

3. **Remove the full content block.**
   Read `chat.meta["full_files_injected"]` to obtain the injection ID, then scan `body["messages"]` for the system message containing `[injection:<id>]` and remove it. There is no reason for the LLM to see the full content block when RAG chunks are being injected by the built-in pipeline — it would only waste context tokens and potentially confuse the model.

When **OFF** (chip disabled, full_files mode active):

- Do nothing. `body` passes through unchanged. Filter A's decisions stand.

---

## 5. How the two filters interact

### 5.1 Sequential execution via priority

Open WebUI processes filters in order of their `Valves.priority` value (`filter.py` — `get_sorted_filter_ids`). The same `form_data` dict is passed sequentially through each filter's `inlet`. Filter B always receives whatever `body` Filter A produced.

This means the filters do not need to communicate directly. Filter A always sets up `full_files` mode. Filter B, when active, undoes what it needs to and lets the RAG pipeline take over.

### 5.2 Example traces

**Trace 1 — User uploads a file, keeps Filter B OFF (full_files)**

```
Turn 1

BEFORE FILTERS:
  body["metadata"]["files"] = [{"id": "abc", "name": "doc.pdf"}]
  body["messages"] = [{"role": "user", "content": "Review this contract"}]

FILTER A (prio=0):
  1. chat.meta["rag_mode_files"] ← [{"id": "abc", "name": "doc.pdf"}]
  2. body["metadata"]["files"] ← []          (cleared)
  3. body["files"] ← {}                       (cleared)
  4. content retrieved from Files.get_file_by_id("abc").data["content"]
  5. body["messages"] ← [system: [injection:abc-...]doc content...,
                         user: "Review this contract"]
     chat.meta["full_files_injected"] ← {"id": "abc-...", "at": ..., "files": [...]}
  6. body["metadata"]["rag_mode"] ← "full_files"

FILTER B (prio=1, OFF):
  passthrough — does nothing

chat_completion_files_handler:
  body["metadata"]["files"] is [] → skipped

Pipe agent_loop_guard:
  rag_mode == "full_files" → removes query_knowledge_files, query_knowledge_bases
  Proxies to gateway
```

```
Turn 2 — same state, user asks a follow-up

BEFORE FILTERS:
  body["metadata"]["files"] = []   (frontend does not re-send file refs)
  body["messages"] = [system: [injection:abc-...]doc content...,
                      user: "Review this contract",
                      assistant: "Here's my analysis...",
                      user: "What about clause 4?"]

FILTER A (prio=0):
  1. body["metadata"]["files"] is empty → no new persistence needed
     chat.meta["rag_mode_files"] still holds the refs from turn 1
  2. body["metadata"]["files"] already empty → no-op
  3. Record exists AND marker [injection:abc-...] found in messages → skip
  4. body["metadata"]["rag_mode"] ← "full_files"

FILTER B (prio=1, OFF):
  passthrough — does nothing

chat_completion_files_handler:
  body["metadata"]["files"] is [] → skipped

Pipe agent_loop_guard:
  rag_mode == "full_files" → removes semantic KB tools
  Proxies to gateway
```

**Trace 2 — User activates Filter B (wants RAG)**

```
Turn 3 — Filter B is now ON

BEFORE FILTERS:
  body["metadata"]["files"] = []   (still empty from frontend)
  body["messages"] = [system: [injection:abc-...]doc content..., ...]

FILTER A (prio=0):
  1. body["metadata"]["files"] is empty → no new persistence
  2. Already empty → no-op
  3. Record + marker found → skip injection
  4. body["metadata"]["rag_mode"] ← "full_files"

FILTER B (prio=1, ON):
  1. chat.meta["rag_mode_files"] → [{"id": "abc", "name": "doc.pdf"}]
     body["metadata"]["files"] ← [{"id": "abc", "name": "doc.pdf"}]
  2. body["metadata"].pop("rag_mode") → key removed
  3. Reads injection id from chat.meta["full_files_injected"],
     scans messages for [injection:abc-...], removes the system message

chat_completion_files_handler:
  body["metadata"]["files"] = [{"id": "abc", "name": "doc.pdf"}] → RUNS RAG!
  Retrieval + reranking runs. Chunks injected.

Pipe agent_loop_guard:
  rag_mode is absent → does not filter tools
  Proxies to gateway
```

**Trace 3 — User switches back to full_files (Filter B OFF again)**

```
Turn 4 — Filter B is OFF

BEFORE FILTERS:
  body["metadata"]["files"] = []
  body["messages"] = [system: <source>chunk1</source>, user: "...", ...]
  (No injection block — Filter B removed it on turn 3)

FILTER A (prio=0):
  1. No files in body → no new persistence
  2. Already empty → no-op
  3. chat.meta["full_files_injected"] exists but marker [injection:...]
     NOT found in messages (was removed) → RE-INJECTS content
  4. body["metadata"]["rag_mode"] ← "full_files"

FILTER B (prio=1, OFF):
  passthrough — does nothing

chat_completion_files_handler:
  body["metadata"]["files"] is [] → skipped

Pipe agent_loop_guard:
  rag_mode == "full_files" → removes semantic KB tools
  Proxies to gateway
```

---

## 6. Pipe: `agent_loop_guard` changes

> **⚠️ Out of scope for this project.** This section is included for architectural completeness only. The Pipe modification will be implemented in the `agent_loop_guard` project, not here.

### 6.1 What the Pipe does today

The `agent_loop_guard` Pipe (`pipes/agent_loop_guard/agent_loop_guard.py`) is an existing, production-ready manifold that:

- Discovers models from a configured gateway via `pipes()`
- Proxies all chat completion requests — streaming and non-streaming — with full auth and custom header support
- Detects and prevents infinite tool-calling loops (escalation: warning → final warning → soft-block)
- Enforces a configurable tool-call budget per turn (runaway protection)
- Filters tools through an admin-configurable blocklist (`TOOL_BLOCKLIST`)
- Modifies `body["tools"]` in-place with slice assignment, and clears entries from `__metadata__["tools"]` to prevent execution

No fundamental change to the Pipe's architecture is needed. It gains one lightweight responsibility.

### 6.2 New responsibility

When `body["metadata"]["rag_mode"] == "full_files"`, the Pipe removes the semantic Knowledge Base search tools from `body["tools"]`. This prevents the LLM from performing semantic retrieval on KBs when full-document mode is active.

### 6.3 Tools affected

Only two tools — the ones that perform semantic queries — are removed. All non-semantic KB browsing tools remain available.

| Tool | In `full_files` mode | In `rag` mode |
|---|---|---|
| `query_knowledge_files` | **Removed** | Available |
| `query_knowledge_bases` | **Removed** | Available |
| `search_knowledge_files` | Available | Available |
| `search_knowledge_bases` | Available | Available |
| `grep_knowledge_files` | Available | Available |
| `view_knowledge_file` | Available | Available |
| `view_file` | Available | Available |
| `list_knowledge` | Available | Available |
| `list_knowledge_bases` | Available | Available |
| `kb_exec` | Available | Available |

This classification is verified against the tool docstrings in `builtin.py`:
- `query_knowledge_files`: "semantic/vector search"
- `search_knowledge_files`: "search by filename"
- `grep_knowledge_files`: "exact string matching"

### 6.4 Integration point

The check runs **after** the existing loop-detection, runaway, and blocklist logic, but **before** the request is forwarded to the gateway. In-place slice assignment (`tools[:] = […]`) is used — the same pattern already proven by the blocklist and loop logic — so the change survives the middleware's shallow copy.

No new valves. The Pipe's `Valves`, `UserValves`, and `pipes()` method remain untouched.

---

## 7. Filter ↔ Pipe coordination

The two filters and the Pipe share no state or direct references. They coordinate through **three** keys on the same mutable objects:

```
chat.meta["rag_mode_files"]          ← Filter A writes, Filter B reads
chat.meta["full_files_injected"]     ← Filter A writes, Filter B reads
body["metadata"]["rag_mode"]         ← Filter A sets, Filter B removes, Pipe reads
```

- **Filter A**: persists file refs to `chat.meta`, clears files from body, injects content (embedding a unique `[injection:<uuid>]` marker), stores the injection record in `chat.meta["full_files_injected"]`, and sets `body["metadata"]["rag_mode"] = "full_files"`. Always.
- **Filter B** (when ON): restores file refs from `chat.meta` into body, removes `rag_mode` from metadata, reads the injection id from `chat.meta["full_files_injected"]`, and removes the system message containing `[injection:<id>]` from messages.
- **Pipe**: reads `body["metadata"].get("rag_mode")`. If the value is `"full_files"`, it removes semantic KB tools. If the key is absent or has any other value, it does nothing.

The Pipe does not need to know which filter set or removed the value. It only needs to know whether the flag is present.

---

## 8. Technical feasibility

All points have been verified against the Open WebUI source code (`main` branch) and official documentation.

### 8.1 Core claims

| Aspect | Status | Evidence |
|---|---|---|
| Inlet runs before `chat_completion_files_handler` | ✅ | `middleware.py` — `process_chat_payload()`: inlet filters at ~line 2430, files handler at ~line 2785 |
| Clearing `body["metadata"]["files"]` suppresses RAG | ✅ | `middleware.py` line 1779: `if files := body.get('metadata', {}).get('files', None)` — if None or empty, skipped |
| Filter priority controls execution order | ✅ | `filter.py` — `get_sorted_filter_ids` sorts by `Valves.priority`; `process_filter_functions` iterates sequentially |
| Full file content accessible in-process | ✅ | `models/files.py` — `File` table stores extracted text under `data.content` |
| `chat.meta` is persistent JSON | ✅ | `models/chats.py` — `Chat.meta` is `Column(JSON, server_default='{}')` |
| `chat_id` available in inlet | ✅ | `filter.py` — `extra_params` includes `__metadata__` and `__chat_id__` |
| Pipe receives and can modify `body` after tool resolution | ✅ | Pipes receive the full `body` dict, including `tools[]` and `metadata` |
| `file_handler` is static (module-level) | ✅ | `filter.py` line 71 — read from module, not per-request. Would prevent mode switching. |
| Execution order is fundamental architecture | ✅ | Filters exist to modify body before handlers consume it |
| Tool classification (semantic vs non-semantic) | ✅ | `builtin.py` — docstrings confirmed for each tool |

### 8.2 Known trade-off: clearing `files[]` vs `file_handler`

The official Open WebUI Filter Function documentation notes:

> *"A naive alternative is to clear `body["metadata"]["files"] = []` inside `inlet()` to suppress the built-in RAG dynamically. This works in practice but is brittle: future Open WebUI versions can add new file/collection plumbing under additional keys. Prefer the documented opt-in `file_handler`."*

This design intentionally uses the "naive" approach. Reasons:

1. **`file_handler` is all-or-nothing.** A filter with `file_handler = True` always skips the built-in RAG. It cannot be toggled per request. To support dynamic switching between `rag` and `full_files`, we would need to reimplement the entire RAG pipeline within the filter — impractical and fragile.

2. **The two-filter architecture sidesteps the three-state problem.** With `file_handler`, a single filter would need to handle: (a) filter disabled → built-in RAG, (b) filter enabled + RAG mode → built-in RAG with tools, (c) filter enabled + full_files mode → custom injection without tools. Three states, two outcomes sharing one outcome's pipeline, and no way to express it with a static module flag.

3. **The two-filter approach maps cleanly onto the available primitives.** Filter A (always-on, no toggle) takes the `full_files` path. Filter B (toggleable) reverses it when enabled. Each filter does one thing. `priority` enforces order. No module flags, no state machines.

4. **The failure mode is non-catastrophic.** If a future Open WebUI version adds new file-plumbing keys, the LLM would receive both RAG chunks (from the new key's handler) and the full content block (from Filter A) — duplicated context, not a crash. The fix is a filter update.

---

## 9. Installation and configuration

### 9.1 Install Filter A — `rag_default_off`

1. **Admin Panel → Functions → Create Function**
2. Type: **Filter**
3. Paste the contents of `rag_default_off.py`
4. Ensure `self.toggle = False` (the filter is always-on, invisible to users)
5. Set `priority = 0` in `Valves`
6. **Model Settings → Filters**: assign this filter to the model that users will use

### 9.2 Install Filter B — `rag_enable`

1. **Admin Panel → Functions → Create Function**
2. Type: **Filter**
3. Paste the contents of `rag_enable.py`
4. Ensure `self.toggle = True` (the filter appears as a chip in the chat UI)
5. Set `priority = 1` in `Valves`
6. **Model Settings → Filters**: assign this filter to the model
7. **Model Settings → Default Filters**: configure `rag_enable` to start **OFF** by default (full_files is the default mode). The user enables it when they want RAG.

### 9.3 Modify the Pipe

> **⚠️ Out of scope for this project.** To be documented and implemented in the `agent_loop_guard` project. The contract is: read `body["metadata"].pop("rag_mode", None)`; if the value is `"full_files"`, remove `query_knowledge_files` and `query_knowledge_bases` from `body["tools"]` using in-place slice assignment.

### 9.4 Combined usage

1. Assign both filters to the model, with priorities 0 and 1
2. Select the **Pipe** (agent_loop_guard sub-model) as the active model in the chat
3. By default, the user gets full-document context. Filter B's chip is OFF.
4. To switch to RAG mode, the user enables Filter B's chip via the Integrations menu (⚙️ icon)
5. To return to full-document mode, the user disables the chip

---

## 10. Edge Cases and Limitations

### 10.1 Context window

`full_files` mode injects the entire file contents. Large files (hundreds of thousands of tokens) may exceed the model's context window. The Filter does not truncate. For very large files, RAG mode remains the recommended option.

### 10.2 Context compaction and content loss

Open WebUI's `compact_messages_for_request` runs **before** filter inlets in `process_chat_payload` (compaction at ~line 2370, filters at ~line 2430). If compaction drops the injected content block, Filter A's `chat.meta["full_files_injected"]` record survives but the `[injection:<id>]` marker is gone from messages. On the next turn, Filter A detects the mismatch (record exists, marker absent) and **re-injects** the content with a new injection id.

The persisted references in `chat.meta["rag_mode_files"]` are unaffected by compaction. Switching to RAG mode from any turn still works — Filter B restores the references and the built-in pipeline takes over.

### 10.3 No files attached

If no files are attached and `chat.meta["rag_mode_files"]` is empty, Filter A has no content to inject and effectively becomes a no-op. Filter B toggling has no visible effect — both modes produce the same outcome.

### 10.4 KB access in `full_files` mode

Non-semantic KB tools (`view_knowledge_file`, `grep_knowledge_files`, `search_knowledge_files`, `list_knowledge`, `kb_exec`) remain available. Only semantic query tools (`query_knowledge_files`, `query_knowledge_bases`) are removed. The user can browse and search KBs by exact match or filename, but cannot perform embedding-based semantic retrieval.

### 10.5 File upload mid-conversation

If the user uploads a new file while `full_files` mode is active, Filter A detects it in `body["metadata"]["files"]`, persists the reference to `chat.meta["rag_mode_files"]`, and injects its content on the next turn. The previous injected content block is left unchanged — the new content is appended.

---

## 11. Summary of design decisions

| Decision | Choice |
|---|---|
| Default mode | `full_files` (full document context) |
| How to get RAG mode | Enable a toggleable filter (`rag_enable`) |
| Two filters vs one | Two: each does one thing, coordinated by `priority` |
| Filter A — `rag_default_off` | Always-on, invisible, prio=0, enforces full_files |
| Filter B — `rag_enable` | Toggleable, visible chip, prio=1, restores RAG when ON |
| Mode switching | User toggles Filter B on/off |
| File reference persistence | `chat.meta["rag_mode_files"]` |
| Injection markers | `[injection:<uuid>]` embedded in content; record in `chat.meta["full_files_injected"]` |
| Re-injection after content loss | Yes — dual check (record + message marker) triggers re-injection |
| RAG suppression mechanism | Pop `files` from `body["metadata"]` and `body` |
| `file_handler` module attribute | Rejected — static, inhibits mode switching |
| Semantic KB tools in `full_files` | Removed by Pipe |
| Non-semantic KB tools in `full_files` | Left available |
| Filter↔Filter coordination | Sequential via `priority` (0 then 1) |
| Filter↔Pipe coordination | `body["metadata"]["rag_mode"]` (Pipe implementation ⚠️ out of scope) |
