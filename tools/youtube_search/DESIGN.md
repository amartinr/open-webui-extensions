# YT DLP API — Open WebUI Tool Reference

This document describes the YT DLP API for implementing an Open WebUI Workspace Tool (Python plugin).

---

## Open WebUI Frontmatter (Metadata Docstring)

Every Open WebUI Workspace Tool requires a top-level docstring with metadata.
Open WebUI (v0.9.6+) auto-fills the **Name**, **ID**, and **Description** fields
in the editor from this docstring when creating a new tool.

The frontmatter must be the very first thing in the Python file (before imports):

```python
"""
title: YouTube Search
id: youtube_search
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: Search YouTube videos, channels, playlists, get transcripts, and more.
required_open_webui_version: 0.5.0
requirements: httpx
version: 1.0.0
licence: MIT
"""
```

| Field | Required | Description |
|---|---|---|
| `title` | Yes | Human-readable tool name |
| `id` | No | Unique slug; auto-generated from title if omitted |
| `description` | Yes | Short description shown in the UI |
| `version` | No | Semantic version |
| `requirements` | No | Comma-separated pip dependencies (installed automatically on import) |
| `required_open_webui_version` | No | Minimum Open WebUI version |
| `author`, `author_url`, `git_url`, `licence` | No | Attribution metadata |

---

## Service URL

The API listens on **port 8700**. Default Valve: `http://localhost:8700/` (configurable via the `api_base_url` valve)

---

## API Endpoints

### `GET /health`

Check service status.

**Response (200):**
```json
{
  "status": "ok",
  "yt_dlp_version": "2026.07.04",
  "deno_available": true,
  "deno_version": "2.4.0",
  "proxy_configured": false,
  "ca_bundle": null,
  "ssl_verification": true
}
```

---

### `GET /search`

Search YouTube by text query. Searches for videos, playlists, or channels.

**Parameters:**

| Name | Type | Required | Default | Constraints | Description |
|---|---|---|---|---|---|
| `query` | string | Yes | — | — | Search term |
| `max_results` | int | No | 10 | 1–20 | Number of results |
| `sort` | string | No | `"relevance"` | `"relevance"`, `"views"`, `"duration"` | Sort order (videos only) |
| `type` | string | No | `"video"` | `"video"`, `"playlist"`, `"channel"` | Content type to search |

**Engine behaviour:**

| `type` | Engine | Notes |
|---|---|---|
| `video` (default) | yt-dlp | Returns full video metadata |
| `playlist` | Deno + InnerTube API | Searches playlists by name |
| `channel` | Deno + InnerTube API | Searches channels by name |

**Response (200) — video type:**
```json
{
  "results": [
    {
      "id": "dQw4w9WgXcQ",
      "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
      "channel": "Rick Astley",
      "views": 1791671680,
      "likes": null,
      "duration": 214,
      "upload_date": "",
      "description": "The official video for...",
      "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg",
      "tags": [],
      "type": "video"
    }
  ]
}
```

**Response (200) — playlist type:**
```json
{
  "results": [
    {
      "type": "playlist",
      "id": "PLE6Wd9FR--EdyJ5lbFl8UuGjecvVw66F6",
      "title": "Machine Learning 2013",
      "channel": "Nando de Freitas",
      "video_count": 21,
      "thumbnail": "https://i.ytimg.com/vi/w2OtwL5T1ow/hq720.jpg"
    }
  ]
}
```

**Response (200) — channel type:**
```json
{
  "results": [
    {
      "type": "channel",
      "id": "UCiuhuf2Xq0d05_4sHG0xmQA",
      "title": "Intuitive Machine Learning",
      "handle": "@IntuitiveMachineLearning",
      "subscriber_count": "22.7K subscribers",
      "thumbnail": "https://yt3.googleusercontent.com/..."
    }
  ]
}
```

**Common fields across all result types:**

| Field | Type | Description |
|---|---|---|
| `id` | string | Video/playlist/channel ID |
| `title` | string | Title |
| `channel` | string | Channel or uploader name |
| `thumbnail` | string | Thumbnail URL |
| `type` | string | `"video"`, `"playlist"`, or `"channel"` |

**No `url` field is returned.** The tool reconstructs YouTube URLs from the `id`:

| Type | URL pattern |
|---|---|
| `video` | `https://www.youtube.com/watch?v={id}` |
| `playlist` | `https://www.youtube.com/playlist?list={id}` |
| `channel` | `https://www.youtube.com/channel/{id}` |

**Notes:**
- `likes` and `upload_date` are **always null/empty** in search results for videos. Use `/video` for those fields.
- The `sort` parameter only applies to `type=video`. For playlists/channels the order is YouTube's default ranking.
- Playlist results include `video_count` (int or null).
- Channel results include `handle` (string starting with @) and `subscriber_count` (string or null).

**Errors:**

| Status | Error code | When |
|---|---|---|
| 400 | `invalid_type` | Unknown `type` value |
| 502 | `search_failed` | Upstream extraction failed |

---

### `GET /video` (alias `GET /metadata`)

Get detailed metadata for a single video.

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `video_id` | string | Yes | YouTube video ID |

`GET /metadata` is a backward-compatible alias.

**Response (200):** Same schema as a search result item, but with real values for `likes` and `upload_date`.

```json
{
  "id": "dQw4w9WgXcQ",
  "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
  "channel": "Rick Astley",
  "views": 1791671680,
  "likes": 19239045,
  "duration": 213,
  "upload_date": "20091025",
  "description": "The official video for...",
  "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg",
  "tags": ["music", "80s"]
}
```

**Error (502):**
```json
{ "error": "metadata_failed", "detail": "..." }
```

---

### `GET /channel`

List videos from a YouTube channel.

**Parameters:**

| Name | Type | Required | Default | Constraints | Description |
|---|---|---|---|---|---|
| `name` | string | Yes | — | — | @handle (e.g. `@statquest`), handle without @ (`statquest`), or UCID |
| `max_results` | int | No | 10 | 1–50 | Maximum videos to return |
| `sort` | string | No | `"views"` | `"views"`, `"date"`, `"duration"` | Sort order |

**Response (200):**
```json
{
  "channel": {
    "id": "UCtYLUTtgS3k1Fg4y5tAhLbw",
    "name": "StatQuest with Josh Starmer",
    "handle": "@statquest",
    "thumbnail": "",
    "subscriber_count": 1660000
  },
  "videos": [
    {
      "id": "h5o1n1QMcmM",
      "title": "Optimization with Linear Programming",
      "views": 11000,
      "duration": 1442,
      "upload_date": "",
      "thumbnail": "https://i.ytimg.com/vi/h5o1n1QMcmM/mqdefault.jpg"
    }
  ]
}
```

**Notes:**
- `upload_date` may be empty (flat extraction for speed). Use `/video` per-video for exact dates.
- The `handle` field is populated for both @handle and UCID queries — yt-dlp extracts it from `uploader_id`. It is only empty if YouTube itself does not return it.

**Errors:**

| Status | Error code | When |
|---|---|---|
| 404 | `channel_not_found` | Channel does not exist |
| 502 | `channel_failed` | Upstream extraction failed |

---

### `GET /playlist`

List videos from a YouTube playlist.

**Parameters:**

| Name | Type | Required | Default | Constraints | Description |
|---|---|---|---|---|---|
| `id` | string | Yes | — | — | Playlist ID (e.g. `PLblh5JKOoLUICTaGLRoHQDuF_7q2GfuJF`) |
| `max_results` | int | No | 10 | 1–50 | Maximum videos to return |

**Response (200):**
```json
{
  "playlist": {
    "id": "PLblh5JKOoLUICTaGLRoHQDuF_7q2GfuJF",
    "title": "Machine Learning",
    "channel": "StatQuest with Josh Starmer",
    "video_count": 106,
    "thumbnail": ""
  },
  "videos": [
    {
      "id": "Gv9_4yMHFhI",
      "title": "A Gentle Introduction to Machine Learning",
      "views": 1500000,
      "duration": 765,
      "channel": "StatQuest with Josh Starmer",
      "thumbnail": "https://i.ytimg.com/vi/Gv9_4yMHFhI/mqdefault.jpg"
    }
  ]
}
```

**Errors:**

| Status | Error code | When |
|---|---|---|
| 404 | `playlist_not_found` | Playlist does not exist |
| 502 | `playlist_failed` | Upstream extraction failed |

---

### `GET /transcript`

Get timed transcript fragments for a video.

**Parameters:**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `video_id` | string | Yes | — | YouTube video ID |
| `language` | string | No | `"en"` | Language code or `"auto"` |

**Language behavior:**
- `"en"`, `"es"`, `"fr"`, etc. → requests that specific language, falls through to any available if not found
- `"auto"` → picks the first available transcript, preferring manually created over auto-generated

**Response (200):** Up to 100 fragments.
```json
{
  "transcript": [
    { "start": 1.4, "text": "[♪♪♪]" },
    { "start": 18.6, "text": "♪ We're no strangers to love ♪" }
  ]
}
```

**Errors:**

| Status | Error code | When |
|---|---|---|
| 404 | `no_transcript` | No transcript exists for the video |
| 404 | `transcripts_disabled` | Transcripts disabled by uploader |
| 502 | `transcript_failed` | Unexpected error |

---

## Error Response Format

All errors follow the same structure:

```json
{
  "error": "<error_code>",
  "detail": "<human-readable description>"
}
```

---

## URL Construction

The API does not return `url` fields. The tool must reconstruct YouTube URLs from the `id`. Use short URLs where possible to minimise token usage:

| Type | Pattern |
|---|---|
| Video | `https://youtu.be/{id}` |
| Playlist | `https://youtube.com/playlist?list={id}` |
| Channel (with handle) | `https://youtube.com/@{handle}` |
| Channel (UCID only) | `https://youtube.com/channel/{ucid}` |

---

## Output Format (Markdown)

The tool must convert the API's JSON response into **Markdown** before returning it to the LLM. LLMs reason better over semantic, pre-formatted text than raw JSON — Markdown eliminates parsing overhead, surfaces only relevant fields, and produces URLs as clickable links.

### General rules

- **URLs** as Markdown links: `[title](url)`. Never return bare `id` fields.
- **Duration** in `m:ss` format (e.g. `765` → `12:45`, `61` → `1:01`).
- **Views, likes** with thousand separators (e.g. `1791658734` → `1,791,658,734`).
- **Upload date** as `YYYY-MM-DD` (e.g. `20091025` → `2009-10-25`).
- **Omit null/empty fields.** Do not include fields like `likes: null` or `tags: []`.
- **Thumbnail** — include the URL as returned by the API. For videos it follows a predictable pattern (`https://i.ytimg.com/vi/{id}/mqdefault.jpg`), for channels it is a non-derivable `yt3.googleusercontent.com` URL. Always pass it through rather than reconstructing it.
- **Description** is truncated to ~200 characters at the list level. Keep full description for `action="video"`.

---

### `search` — videos

```markdown
## Search results for "{query}"

### 1. [{title}](https://youtu.be/{id})
- **Channel:** {channel}
- **Views:** {views:,}
- **Duration:** {m:ss}
- **Thumbnail:** {thumbnail}
- **Description:** {description[:200]...}

### 2. [{title}](https://youtu.be/{id})
...
```

### `search` — playlists

```markdown
## Search results for "{query}" (playlists)

### 1. [{title}](https://youtube.com/playlist?list={id})
- **Channel:** {channel}
- **Videos:** {video_count}
- **Thumbnail:** {thumbnail}

### 2. [{title}](https://youtube.com/playlist?list={id})
...
```

### `search` — channels

```markdown
## Search results for "{query}" (channels)

### 1. [{title}](https://youtube.com/@{handle})
- **Subscribers:** {subscriber_count}
- **Thumbnail:** {thumbnail}

### 2. [{title}](https://youtube.com/@{handle})
...
```

If `handle` is not available, fall back to `https://youtube.com/channel/{id}`.

---

### `video`

```markdown
## [{title}](https://youtu.be/{id})

| Field | Value |
|---|---|
| **Channel** | {channel} |
| **Views** | {views:,} |
| **Likes** | {likes:,} |
| **Duration** | {m:ss} |
| **Published** | {upload_date: YYYY-MM-DD} |
| **Tags** | {tags, join: ", "} |
| **Thumbnail** | {thumbnail} |
| **URL** | https://youtu.be/{id} |

**Description:**
{description}
```

Omits rows for any null/empty field (e.g. no `Likes` row if `likes` is null).

---

### `channel`

```markdown
## {name}
- **Handle:** @{handle}
- **Subscribers:** {subscriber_count}
- **URL:** https://youtube.com/@{handle}

### Videos

| # | Title | Views | Duration |
|---|---|---|---|
| 1 | [{title}](https://youtu.be/{id}) | {views:,} | {m:ss} |
| 2 | [{title}](https://youtu.be/{id}) | {views:,} | {m:ss} |
```

---

### `playlist`

```markdown
## {title}
- **Channel:** {channel}
- **Videos:** {video_count}
- **URL:** https://youtube.com/playlist?list={id}

### Videos

| # | Title | Views | Duration |
|---|---|---|---|
| 1 | [{title}](https://youtu.be/{id}) | {views:,} | {m:ss} |
| 2 | [{title}](https://youtu.be/{id}) | {views:,} | {m:ss} |
```

---

### `transcript`

```markdown
## Transcript

| Time | Text |
|---|---|
| 0:01 | [♪♪♪] |
| 0:18 | ♪ We're no strangers to love ♪ |
```

Timestamps use `m:ss` format (e.g. `1.4` → `0:01`, `120.0` → `2:00`).

---

### `health`

```markdown
## Service Status

- **Status:** ok
- **yt-dlp version:** 2026.07.04
- **Deno version:** 2.4.0
- **Proxy configured:** no
- **SSL verification:** yes
```

---

### Error

```markdown
**Error:** {error_code}
{detail}
```

Example:

```markdown
**Error:** channel_not_found
Channel not found: @invalid
```

---

## Open WebUI Tool Structure

The tool is defined as a **class** (not a bare function). Every public method on
the class is exposed to the LLM as a callable tool. However, to simplify the
LLM's decision space, this tool exposes a **single method** with an `action`
parameter that internally dispatches to the correct endpoint.

### Valves (admin — Workspace > Tools)

Infrastructure configuration set once by the administrator.

| Valve | Default | Bounds | Description |
|---|---|---|---|
| `api_base_url` | `"http://localhost:8700/"` | — | Base URL of the YT DLP API service |
| `request_timeout` | `30` | — | HTTP request timeout in seconds |
| `max_results` | `20` | ≥ 1 | **Global ceiling.** No endpoint returns more results than this. |

### UserValves (user — Chat > ⚙️)

Personal preferences that each user configures from their chat session.

| UserValve | Default | Bounds | Description |
|---|---|---|---|
| `preferred_language` | `"en"` | — | Default language for transcripts |
| `region` | `""` | — | Optional region filter (e.g. `"ES"`, `"US"`, `"MX"`). Empty = no filter |
| `default_results` | `10` | 1 – `max_results` | Default results when the LLM doesn't specify `max_results` |
| `max_results` | `10` | 1 – `max_results` (admin) | **Personal ceiling.** Cannot exceed the admin valve |

**Invariant:** `default_results ≤ user.max_results ≤ admin.max_results`

### Parameter resolution logic

**Language** — the UserValve is a fallback, not a limit:

```python
lang = language if language is not None else self.user_valves.preferred_language
```

**Results** — the UserValve `max_results` is a limit the LLM cannot exceed:

```python
def resolve_max_results(self, llm_param: int | None) -> int:
    # If the LLM didn't pass anything, use the user default
    base = llm_param if llm_param is not None else self.user_valves.default_results

    # Clamp by personal ceiling and admin ceiling
    return min(base, self.user_valves.max_results, self.valves.max_results)
```

| Nature | Parameter | UserValve | Behaviour |
|---|---|---|---|
| **Fallback** | `language` | `preferred_language` | If the LLM omits it, the UserValve is used. If the LLM specifies it, that takes precedence. |
| **Limit** | `max_results` | `default_results` + `max_results` | If the LLM omits it, `default_results` is used. If the LLM specifies it, it is honoured but capped by `max_results` (personal) and `max_results` (admin). |

### Class structure

```python
from pydantic import BaseModel, Field
from typing import Optional
import httpx


class Tools:
    class Valves(BaseModel):
        api_base_url: str = Field(
            default="http://localhost:8700/",
            description="Base URL of the YT DLP API service",
        )
        request_timeout: int = Field(
            default=30,
            description="HTTP request timeout in seconds",
        )
        max_results: int = Field(
            default=20,
            description="Hard limit on results.",
            ge=1,
        )

    class UserValves(BaseModel):
        preferred_language: str = Field(
            default="en",
            description="Default language for transcripts",
        )
        region: str = Field(
            default="",
            description="Optional region filter (e.g. ES, US, MX). Empty = no filter",
        )
        default_results: int = Field(
            default=10,
            description="Default results when the LLM doesn't specify max_results",
            ge=1,
        )
        max_results: int = Field(
            default=10,
            description="Personal cap on results. Cannot exceed admin's max_results.",
            ge=1,
        )

    def __init__(self):
        self.valves = self.Valves()
        self.user_valves = self.UserValves()
```

### Method signature (async)

Open WebUI recomienda `async def` para compatibilidad con versiones futuras
completamente asíncronas del engine:

```python
async def youtube_tool(
    self,
    action: str,              # Required. One of: search, video, channel, playlist, transcript, health
    query: str = "",          # For action=search: search term
    video_id: str = "",       # For action=video|transcript: YouTube video ID
    channel_name: str = "",   # For action=channel: @handle, handle, or UCID
    playlist_id: str = "",    # For action=playlist: playlist ID
    max_results: Optional[int] = None,  # If omitted, falls back to UserValve default_results
    sort: str = "relevance",  # For search|channel: sort order (relevance, views, date, duration)
    search_type: str = "video", # For action=search: video, playlist, or channel
    language: Optional[str] = None,  # If omitted, falls back to UserValve preferred_language
    __event_emitter__=None,   # Injected by Open WebUI for status/progress events
) -> str:
    """
    Unified tool for querying YouTube via the YT DLP API.

    Dispatches to the correct API endpoint based on `action` and returns
    formatted Markdown.

    :param action: One of: search, video, channel, playlist, transcript, health
    :param query: Search term (required for action=search)
    :param video_id: YouTube video ID (required for action=video|transcript)
    :param channel_name: @handle, handle, or UCID (required for action=channel)
    :param playlist_id: Playlist ID (required for action=playlist)
    :param max_results: Results requested by the LLM.
        If omitted, falls back to UserValve default_results.
        In any case, clamped by UserValve max_results (personal ceiling)
        and AdminValve max_results (global ceiling).
    :param sort: Sort order (relevance, views, date, duration)
    :param search_type: Content type for search (video, playlist, channel)
    :param language: Language code for transcript.
        If omitted, falls back to UserValve preferred_language.
        The UserValve does not override the LLM: if the agent passes an
        explicit language, that takes precedence.
    :param __event_emitter__: Injected by Open WebUI for emitting status events
    """
```

### Event emitter usage

The `__event_emitter__` parameter is injected automatically by Open WebUI. Use
it to surface progress updates in the chat UI. All event types used below are
**compatible with Native Mode**:

```python
# Simple status update (spinner in the UI)
await __event_emitter__(
    {
        "type": "status",
        "data": {"description": "Searching YouTube...", "done": False},
    }
)

# Clear the spinner when done
await __event_emitter__(
    {
        "type": "status",
        "data": {"description": "Searching YouTube...", "done": True},
    }
)

# Notification for errors/warnings
await __event_emitter__(
    {
        "type": "notification",
        "data": {"message": "Channel not found", "type": "error"},
    }
)
```

**When to emit events:**
- Emit a `status` event at the start of each action (e.g. `"Fetching video metadata..."`)
- Emit a `status` with `done: True` when the API call completes
- Emit a `notification` with `type: "error"` on API errors
- Emit a `notification` with `type: "warning"` on recoverable issues (e.g. transcript not found, falling back to default language)

### Actions

| `action` | Required params | Optional params | Calls | Returns |
|---|---|---|---|---|
| `search` | `query` | `max_results`, `sort`, `search_type` | `GET /search` | List of results with `id`, `title`, `type`, etc. |
| `video` | `video_id` | — | `GET /video` | Full video metadata (likes, date, tags) |
| `channel` | `channel_name` | `max_results`, `sort` | `GET /channel` | Channel info + list of videos |
| `playlist` | `playlist_id` | `max_results` | `GET /playlist` | Playlist info + list of videos |
| `transcript` | `video_id` | `language` | `GET /transcript` | Timed transcript fragments |
| `health` | — | — | `GET /health` | Service status |

### Error handling

On error the API returns a JSON body with `error` and `detail` fields:

```json
{ "error": "channel_not_found", "detail": "Channel not found: @invalid" }
```

The tool should always check for the presence of an `error` field in the response.
On errors:
1. Emit a `notification` event with `type: "error"`
2. Return the error as formatted Markdown (see [Error](#error) output template)

---

## Typical Usage Flows

### Flow 1: Search + metadata (videos)
1. `youtube_tool(action="search", query="rick astley", max_results=3)`
2. If likes or upload date needed → `youtube_tool(action="video", video_id="dQw4w9WgXcQ")`

### Flow 2: Search non-video content
1. `youtube_tool(action="search", query="machine learning", max_results=3, search_type="playlist")`
2. Get playlist contents → `youtube_tool(action="playlist", playlist_id="PL...", max_results=10)`

1. `youtube_tool(action="search", query="python", max_results=3, search_type="channel")`
2. Get channel videos → `youtube_tool(action="channel", channel_name="@Fireship", max_results=10)`

### Flow 3: Explore channel or playlist
1. `youtube_tool(action="channel", channel_name="@statquest", max_results=5, sort="views")`
2. Get details of one video → `youtube_tool(action="video", video_id="h5o1n1QMcmM")`
3. Get transcript → `youtube_tool(action="transcript", video_id="h5o1n1QMcmM", language="en")`

### Flow 4: Summarise
1. `youtube_tool(action="transcript", video_id="dQw4w9WgXcQ", language="en")`
2. LLM summarises the transcript text

---

## Example User Prompts

| What the user asks | What the tool does |
|---|---|
| "Search for Rick Astley videos" | `youtube_tool(action="search", query="rick astley", max_results=5)` |
| "Find the most viewed ones on this topic" | `youtube_tool(action="search", query="topic", max_results=5, sort="views")` |
| "Show the longest videos about..." | `youtube_tool(action="search", query="...", max_results=5, sort="duration")` |
| "Find playlists about machine learning" | `youtube_tool(action="search", query="machine learning", max_results=5, search_type="playlist")` |
| "Find channels that teach Python" | `youtube_tool(action="search", query="python", max_results=5, search_type="channel")` |
| "Show me what's on the @Fireship channel" | `youtube_tool(action="channel", channel_name="@Fireship", max_results=10, sort="views")` |
| "List videos in this playlist..." | `youtube_tool(action="playlist", playlist_id="PLblh5JKOoLU...", max_results=20)` |
| "How many views does this video have?" | `youtube_tool(action="video", video_id="dQw4w9WgXcQ")` → `views` |
| "When was it published?" | `youtube_tool(action="video", video_id="dQw4w9WgXcQ")` → `upload_date` |
| "Who uploaded it?" | `youtube_tool(action="video", video_id="dQw4w9WgXcQ")` → `channel` |
| "What is this video about?" | `youtube_tool(action="video", video_id="dQw4w9WgXcQ")` → `description` |
| "How long is it?" | `youtube_tool(action="video", ...)` or search → `duration` |
| "Summarise this video" | `youtube_tool(action="transcript", video_id="...", language="en")` then LLM summarises |
| "What did they say at minute 2?" | `youtube_tool(action="transcript", video_id="...", language="en")` → filter around 120s |
| "Give me the link to that video" | Reconstruct URL: `https://www.youtube.com/watch?v={id}` |

**Flow tips for the LLM:**
- For lists with sorting (`views`, `duration`), present results ordered from highest to lowest.
- `likes` and `upload_date` are only available via `action="video"`, not via search. If the user asks about them, always call `action="video"`.
- When the user provides a video URL, extract the 11-character video ID after `v=` and use it as `video_id`.
- The API does not return `url` fields. Always construct URLs using the patterns in the URL Construction section.
- Playlist and channel search results don't include per-video stats. Use `action="playlist"` or `action="channel"` to get those.
- `channel_name` accepts @handle (`@statquest`), handle without @ (`statquest`), or UCID.

---

## Implementation Notes

### Async HTTP with httpx

Use `httpx.AsyncClient` instead of `urllib`:

```python
async with httpx.AsyncClient(timeout=self.valves.request_timeout) as client:
    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
```

- `httpx` is already available in most Open WebUI environments (it is a transitive
dependency of the platform itself) and is listed in `requirements:` in the
frontmatter for explicit tracking.
- `AsyncClient` keeps the event loop free while waiting for the API response.
- Setting `timeout` from `self.valves.request_timeout` makes it configurable.

### Error handling

The API returns 4xx/5xx with a JSON body. Use `response.raise_for_status()`
for HTTP-level errors, then parse the JSON error object:

```python
try:
    async with httpx.AsyncClient(timeout=...) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            # API-level error (e.g. 404 with JSON body)
            return format_error(data["error"], data.get("detail", ""))
        return format_success(action, data)
except httpx.HTTPStatusError as e:
    # HTTP-level error
    return format_error("http_error", str(e))
except httpx.TimeoutException:
    return format_error("timeout", "Request timed out")
except Exception as e:
    return format_error("unexpected_error", str(e))
```

### Output format

Convert the API response to Markdown following the templates in the
[Output Format (Markdown)](#output-format-markdown) section. **Do not return raw
JSON to the LLM.**

Ver la resolución de `max_results` y `language` en la sección
[Lógica de resolución de parámetros](#lógica-de-resolución-de-parámetros).

### URLs

Never rely on the API returning `url` fields. Always construct them from `id` +
type using the short URL patterns in the [URL Construction](#url-construction)
section.

### Event emitter pattern

Keep event emission consistent across all actions:

```python
async def _emit_status(self, __event_emitter__, description: str, done: bool = False):
    if __event_emitter__:
        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": description, "done": done},
            }
        )

async def _emit_notification(self, __event_emitter__, message: str, level: str = "info"):
    if __event_emitter__:
        await __event_emitter__(
            {
                "type": "notification",
                "data": {"message": message, "type": level},
            }
        )
```

This keeps the main method clean and avoids repeating `if __event_emitter__` checks.
