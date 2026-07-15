# Implementation Plan — Smart Fetch URL Improvements

**Branch:** `refactor/smart_fetch_url_valves`
**Target file:** `smart_fetch_url.py` (+ `test/skimmd.py` for inline parser)
**Version:** 0.3-draft

---

## Overview

Three sequential proposals. Each returns control to the user for review.

---

## Implementation Order

```
✅ #1 — Remove remove_images + permanent base64 stripping
⬜ #2 — default_browser as fixed selector
⬜ #3 — blocked_domains (domain blocklist)
```

---

## Proposal #1 — Remove `remove_images` + permanent base64 stripping ✅

**Status: Done**

### Problem (base64)

Many pages embed images as `data:image/png;base64,iVBORw0KGgo...` directly
in the HTML. A single image can consume tens of thousands of characters.
The LLM cannot render these, so the base64 URL is pure token waste.

### Problem (`remove_images`)

The `remove_images` parameter was:
1. **Broken in `skimmd` format** (the default) — the skimmd parser ignored it,
   always emitting `![alt](url)`.
2. **Destructive in other formats** — `remove_images=True` called
   `node.decompose()`, losing the alt text along with the image.
3. **Unnecessary for token savings** — normal image URLs (`https://...`) are
   short (~60-120 chars) and often semantically meaningful to the LLM.
   The real token waste is base64, not normal URLs.

### Solution

- **Removed `remove_images` parameter** entirely from the tool signature,
  all internal methods, and Valve definitions.
- **Normal image URLs** are always included as `![alt](url)` — cheap and
  informative.
- **Base64 is always stripped** to `![alt]()` — regardless of any flag,
  because base64 is always pure token waste.

### Behaviour

| Situation | Output |
|---|---|
| Normal URL with alt | `![Cat](https://example.com/cat.jpg)` |
| Normal URL without alt | `![](https://example.com/foto.jpg)` |
| Base64 with alt | `![Cat]()` |
| Base64 without alt | `![]()` |
| Poster image (base64) | `![Poster]()` |
| Video source (base64) | `[Video]` |

### Files affected

- `smart_fetch_url.py`
- `test/skimmd.py`

---

## Proposal #2 — `default_browser` as fixed selector ⬜

### Problem

`default_browser` is a free-text field where any value can be entered,
including invalid ones or version-specific profiles (e.g. `firefox_147`)
that may break when `curl_cffi` updates.

### Solution

Convert the field to a closed selector with the 4 generic aliases that
`curl_cffi` auto-resolves to the latest version.

### Valves

#### Admin (`Valves`)

`default_browser: Literal["firefox", "chrome", "edge", "safari"]`

Rendered as a dropdown in the Open WebUI admin panel.

#### User (`UserValves`)

`default_browser: Optional[Literal["firefox", "chrome", "edge", "safari"]]`

Rendered as a dropdown with an additional "Inherit from admin" option.

### Behaviour

- Only the 4 generic aliases are accepted.
- `curl_cffi` resolves each to the latest version automatically.
- Internal validation against the full `BROWSER_PROFILES` set is simplified.
- The `smart_fetch_url()` `browser` parameter type is also narrowed.

### Files affected

- `smart_fetch_url.py` only (Valve types, parameter types, validation logic)

### Verification

- Admin dropdown shows only `firefox`, `chrome`, `edge`, `safari`
- User dropdown shows the same 4 + inherit option
- Passing `firefox_147` as browser raises validation error
- All existing fetch logic works identically

---

## Proposal #3 — `blocked_domains` domain blocklist ⬜

### Problem

The model can request URLs from sites that consume hundreds of thousands of
tokens (YouTube, TikTok, Reddit, etc.), exhausting context and lengthening
responses. There is no way to block this at the tool level.

### Solution

Add a two-level domain blocking system (admin + user) with an informative
error when a blocked domain is requested.

### Valves

#### Admin (`Valves`)

| Field | Type | Default | Description |
|---|---|---|---|
| `blocked_domains` | `str` | `""` | Comma/newline-separated domain list. `*.example.com` blocks all subdomains. |

#### User (`UserValves`)

| Field | Type | Default | Description |
|---|---|---|---|
| `blocked_domains` | `Optional[str]` | `None` | Domains the user wants to add to the blocklist. |

### Behaviour

- **Additive resolution:** User domains are **added** to admin domains, not
  replaced. If admin blocks `youtube.com` and user adds `tiktok.com`, both
  are blocked.
- **Pattern format:**
  - `youtube.com` — matches that domain and any subdomain
    (`www.youtube.com`, `m.youtube.com`).
  - `*.tiktok.com` — matches any subdomain of tiktok.com.
  - Case-insensitive.
- **Check timing:** Before any HTTP connection, for both single and batch URLs.
- **Response:** Structured error with `status_code=403`,
  `error_type="forbidden"`, clear message stating the blocked domain.
- **Batch behaviour:** Each URL checked individually. Blocked ones get error,
  valid ones proceed normally.

### Files affected

- `smart_fetch_url.py` (Valves + new method + check in `smart_fetch_url`)

### Verification

- `youtube.com` blocked → `https://www.youtube.com/watch?v=...` returns 403 error
- `youtube.com` blocked → `https://youtube.com/feed` returns 403 error
- `*.tiktok.com` blocked → `https://tiktok.com/@user` returns 403 error
- Unblocked domain works normally
- Batch: mix of blocked + unblocked URLs → blocked get errors, others succeed

---

## Risk & Mitigation

| Risk | Mitigation |
|---|---|
| #2 breaks existing admin configs | Admin re-saves dropdown value |
| #1 regex false positive | `data:` only matches inside valid image syntax patterns |
| #3 false positive on subdomain matching | `fnmatch` with `*` prefix; well-tested in stdlib |

## Rollback

Each proposal is a single atomic commit on the branch. Rollback = revert that
commit. No cascading dependencies between proposals.
