# Smart Fetch URL

An Open WebUI tool for fetching URLs with browser-grade TLS fingerprinting and clean content extraction.

Inspired by [pi-smart-fetch](https://pi.dev/packages/pi-smart-fetch), a web fetching extension for [pi.dev](https://pi.dev).

## Features

- **TLS fingerprinting** — impersonates real browsers (Chrome, Firefox, Safari, Edge) via `curl_cffi`
- **Defuddle-style extraction** — clean readable content via `trafilatura`
- **Rich metadata** — title, author, site, language, published date
- **Alternate content fallback** — follows `<link rel="alternate">` when extraction yields thin content
- **Batch fetch** — multiple URLs with bounded concurrency
- **Multiple output formats** — markdown, html, text, json, raw
- **UserValves** — per-user overrides for all config settings (max_chars, timeout, browser, concurrency) from the chat session

## Requirements

Installed automatically by Open WebUI on first load:

- `curl_cffi` — TLS/HTTP2 fingerprinting
- `trafilatura` — content extraction
- `selectolax` — HTML parsing fallback

## Usage

Import into Open WebUI at **Workspace → Tools → +** and attach to a model.

### `smart_fetch_url`

```
smart_fetch_url(url, format?, max_chars?, browser?, os?, timeout_ms?,
                remove_images?, include_replies?, proxy?, headers?)
```

Configuration values are resolved with the following precedence:
**method argument > UserValve (chat) > admin Valve > global default**.

### `batch_fetch_urls`

```
batch_fetch_urls(urls, format?, max_chars?, browser?, os?,
                 timeout_ms?, concurrency?)
```

### UserValves (per-user, configurable from chat)

| Field | Type | Description |
|---|---|---|
| `max_chars` | `int` | Maximum characters to return |
| `timeout_ms` | `int` | Request timeout in milliseconds |
| `default_browser` | `str` | Browser fingerprint profile |
| `batch_concurrency` | `int` | Concurrency for batch fetches |
| `verbose` | `bool` | Emit detailed status events |

## License

MIT
