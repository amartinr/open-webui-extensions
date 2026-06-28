# Smart Fetch URL

An Open WebUI tool for fetching URLs with browser-grade TLS fingerprinting and clean content extraction.

A Python port of [pi-smart-fetch](https://pi.dev/packages/pi-smart-fetch) by [Thinkscape](https://github.com/Thinkscape/agent-smart-fetch), adapted for Open WebUI.

## Features

- **TLS fingerprinting** - impersonates real browsers (Chrome, Firefox, Safari, Edge) via `curl_cffi`
- **Content-type detection** - classifies content as article, feed (RSS/Atom), or listing (forums, link aggregators) and uses the right extractor for each
- **Smart Content-Type routing** - handles binary files, extractable documents (PDF, DOCX), and text/HTML with different code paths
- **Rich metadata** - title, author, site, language, published date
- **Alternate content fallback** - follows `<link rel="alternate">` when extraction yields thin content
- **Batch fetch** - multiple URLs with bounded concurrency
- **Multiple output formats** - markdown, html, text, json, raw, skimmd
- **UserValves** - per-user overrides for all config settings (max_chars, timeout, browser, concurrency) from the chat session

## Requirements

Installed automatically by Open WebUI on first load:

- `curl_cffi` - TLS/HTTP2 fingerprinting
- `trafilatura` - content extraction
- `selectolax` - HTML parsing fallback

## Usage

Import into Open WebUI at **Workspace → Tools → +** and attach to a model.

### `smart_fetch_url`

```
smart_fetch_url(url, format?, max_chars?, browser?, os_profile?, timeout_ms?,
                remove_images?, include_replies?, proxy?, headers?, show_favicons?)
```

Configuration values are resolved with the following precedence:
**method argument > UserValve (chat) > admin Valve > global default**.

### `batch_fetch_urls`

```
batch_fetch_urls(urls, format?, max_chars?, browser?, os_profile?,
                 timeout_ms?, concurrency?, remove_images?, include_replies?, headers?)
```

## Output Formats

| Format | Description | Use case |
|---|---|---|
| `markdown` | Clean text via trafilatura (default) | Articles, blog posts |
| `html` | Lightly cleaned HTML | When structure matters |
| `txt` | Plain text, no formatting | Minimal token usage |
| `json` | Structured output with metadata | Programmatic consumption |
| `raw` | Full unprocessed server response | Debugging, passthrough |
| **`skimmd`** | Skimmed Markdown — whitelist-based HTML-to-MD converter | Feeds, listings, media-rich pages |

### `skimmd` — Skimmed Markdown

Preserves **all links, images, and videos** while stripping navigation, scripts,
and structural noise. Ideal for Reddit frontpages, forum threads, search results,
galleries, or any page where trafilatura's article extraction is too aggressive.

- **Zero external dependencies** — uses only stdlib (`html.parser`, `re`, `urllib`)
- **Whitelist-based** — only known-safe tags survive; everything else is stripped
- **`strip_external=True`** — blocks containing only external links are discarded;
  mixed blocks keep internal content and drop external anchor text
- **Inline in `smart_fetch_url.py`** — no separate import needed when pasted into
  Open WebUI

### UserValves (per-user, configurable from chat)

| Field | Type | Description |
|---|---|---|
| `max_chars` | `int` | Maximum characters to return |
| `timeout_ms` | `int` | Request timeout in milliseconds |
| `default_browser` | `str` | Browser fingerprint profile |
| `batch_concurrency` | `int` | Concurrency for batch fetches |
| `verbose` | `bool` | Emit detailed status events |

## License

MIT - see [LICENSE](./LICENSE).

This project is a derivative of [pi-smart-fetch](https://pi.dev/packages/pi-smart-fetch) by Thinkscape, also MIT licensed.
