"""
title: YouTube Search
id: youtube_search
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: Search YouTube videos, channels, playlists, get transcripts, and embed videos inline in the chat.
required_open_webui_version: 0.5.0
requirements: httpx
version: 1.2.0
licence: MIT
"""

import re
from typing import Optional
import html as _html

import httpx
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse


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

    # ------------------------------------------------------------------ #
    # Event emitter helpers
    # ------------------------------------------------------------------ #

    async def _emit_status(
        self, __event_emitter__, description: str, done: bool = False
    ):
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": description, "done": done},
                }
            )

    async def _emit_notification(
        self, __event_emitter__, message: str, level: str = "info"
    ):
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "notification",
                    "data": {"content": message, "type": level},
                }
            )

    # ------------------------------------------------------------------ #
    # Parameter resolution
    # ------------------------------------------------------------------ #

    def _resolve_max_results(self, llm_param: Optional[int]) -> int:
        base = (
            llm_param
            if llm_param is not None
            else self.user_valves.default_results
        )
        return min(
            base,
            self.user_valves.max_results,
            self.valves.max_results,
        )

    # ------------------------------------------------------------------ #
    # URL / params builder
    # ------------------------------------------------------------------ #

    def _build_request(self, action: str, **kwargs) -> tuple[str, dict]:
        base = self.valves.api_base_url.rstrip("/")
        rtype = kwargs.get("type", "video")

        if action == "search":
            params = {
                "query": kwargs["query"],
                "max_results": self._resolve_max_results(kwargs.get("max_results")),
                "type": rtype,
            }
            if kwargs.get("sort") and rtype in ("video", ""):
                params["sort"] = kwargs["sort"]
            return f"{base}/search", params

        elif action == "get":
            if rtype == "video":
                return f"{base}/video", {"video_id": kwargs["id"]}
            elif rtype == "transcript":
                params = {"video_id": kwargs["id"]}
                lang = (
                    kwargs.get("language")
                    if kwargs.get("language") is not None
                    else self.user_valves.preferred_language
                )
                if lang:
                    params["language"] = lang
                return f"{base}/transcript", params

        elif action == "list":
            if rtype == "channel":
                params = {
                    "name": kwargs["id"],
                    "max_results": self._resolve_max_results(kwargs.get("max_results")),
                }
                channel_sorts = ("views", "date", "duration")
                if kwargs.get("sort") in channel_sorts:
                    params["sort"] = kwargs["sort"]
                return f"{base}/channel", params
            elif rtype == "playlist":
                params = {
                    "id": kwargs["id"],
                    "max_results": self._resolve_max_results(kwargs.get("max_results")),
                }
                return f"{base}/playlist", params

        raise ValueError(f"Unknown action/type: {action}/{rtype}")

    # ------------------------------------------------------------------ #
    # HTTP call
    # ------------------------------------------------------------------ #

    async def _call_api(
        self, url: str, params: dict, __event_emitter__
    ) -> dict:
        timeout = self.valves.request_timeout
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except httpx.TimeoutException:
                await self._emit_notification(
                    __event_emitter__, "Request timed out", "error"
                )
                return {"error": "timeout", "detail": "Request timed out"}
            except httpx.HTTPStatusError as e:
                try:
                    body = e.response.json()
                    err = body.get("error", "http_error")
                    detail = body.get("detail", str(e))
                except Exception:
                    err = "http_error"
                    detail = str(e)
                await self._emit_notification(
                    __event_emitter__, detail, "error"
                )
                return {"error": err, "detail": detail}
            except Exception as e:
                await self._emit_notification(
                    __event_emitter__, str(e), "error"
                )
                return {"error": "unexpected_error", "detail": str(e)}

        if "error" in data:
            await self._emit_notification(
                __event_emitter__, data.get("detail", data["error"]), "error"
            )

        return data

    # ------------------------------------------------------------------ #
    # Markdown formatters
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _fmt_number(n: int) -> str:
        return str(n)

    @staticmethod
    def _fmt_date(yyyymmdd: Optional[str]) -> str:
        if not yyyymmdd:
            return ""
        return yyyymmdd

    @staticmethod
    def _fmt_url(item: dict, default_type: str = "") -> str:
        t = item.get("type", default_type)
        id_ = item.get("id", "")
        if t == "video":
            return f"https://youtu.be/{id_}"
        elif t == "playlist":
            return f"https://youtube.com/playlist?list={id_}"
        elif t == "channel":
            handle = item.get("handle", "")
            if handle:
                return f"https://youtube.com/{handle}"
            return f"https://youtube.com/channel/{id_}"
        return ""

    def _fmt_search_videos(self, query: str, results: list) -> str:
        lines = [f"## Search results for \"{query}\""]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = self._fmt_url(r)
            channel = r.get("channel", "")
            views = r.get("views")
            duration = r.get("duration")
            upload_date = r.get("upload_date", "")
            thumb = r.get("thumbnail", "")
            desc = r.get("description", "")

            lines.append(f"\n### {i}. [{title}]({url})")
            if channel:
                lines.append(f"- **Channel:** {channel}")
            if views is not None:
                lines.append(f"- **Views:** {self._fmt_number(views)}")
            if duration is not None:
                lines.append(f"- **Duration:** {self._fmt_duration(duration)}")
            if upload_date:
                lines.append(f"- **Published:** {self._fmt_date(upload_date)}")
            if thumb:
                lines.append(f"- **Thumbnail:** {thumb}")
            if desc:
                lines.append(f"- **Description:** {desc[:200]}{'...' if len(desc) > 200 else ''}")
        return "\n".join(lines)

    def _fmt_search_playlists(self, query: str, results: list) -> str:
        lines = [f"## Search results for \"{query}\" (playlists)"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = self._fmt_url(r)
            channel = r.get("channel", "")
            vcount = r.get("video_count")
            thumb = r.get("thumbnail", "")

            lines.append(f"\n### {i}. [{title}]({url})")
            if channel:
                lines.append(f"- **Channel:** {channel}")
            if vcount is not None:
                lines.append(f"- **Videos:** {vcount}")
            if thumb:
                lines.append(f"- **Thumbnail:** {thumb}")
        return "\n".join(lines)

    def _fmt_search_channels(self, query: str, results: list) -> str:
        lines = [f"## Search results for \"{query}\" (channels)"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = self._fmt_url(r)
            handle = r.get("handle", "")
            subs = r.get("subscriber_count")
            thumb = r.get("thumbnail", "")

            lines.append(f"\n### {i}. [{title}]({url})")
            if handle:
                lines.append(f"- **Handle:** {handle}")
            if subs is not None:
                lines.append(f"- **Subscribers:** {self._fmt_number(subs)}")
            if thumb:
                lines.append(f"- **Thumbnail:** {thumb}")
        return "\n".join(lines)

    def _fmt_video(self, data: dict) -> str:
        title = data.get("title", "Untitled")
        url = self._fmt_url(data, default_type="video")
        lines = [f"## [{title}]({url})", "", "| Field | Value |", "|---|---|"]

        pairs = [
            ("Channel", data.get("channel")),
            ("Views", self._fmt_number(data["views"]) if data.get("views") is not None else None),
            ("Likes", self._fmt_number(data["likes"]) if data.get("likes") is not None else None),
            ("Duration", self._fmt_duration(data["duration"]) if data.get("duration") is not None else None),
            ("Published", self._fmt_date(data.get("upload_date", "")) if data.get("upload_date") else None),
            ("Tags", ", ".join(data["tags"]) if data.get("tags") else None),
            ("Thumbnail", data.get("thumbnail")),
            ("URL", url),
        ]

        for label, value in pairs:
            if value:
                lines.append(f"| **{label}** | {value} |")

        desc = data.get("description", "")
        if desc:
            lines.extend(["", "**Description:**", desc])

        return "\n".join(lines)

    def _fmt_channel(self, data: dict) -> str:
        chan = data.get("channel", {})
        videos = data.get("videos", [])
        name = chan.get("name", "")
        handle = chan.get("handle", "")
        subs = chan.get("subscriber_count")
        url = f"https://youtube.com/{handle}" if handle else ""

        lines = [f"## {name}"]
        if handle:
            lines.append(f"- **Handle:** {handle}")
        if subs is not None:
            lines.append(f"- **Subscribers:** {self._fmt_number(subs)}")
        if url:
            lines.append(f"- **URL:** {url}")

        if videos:
            lines.extend(["", "### Videos", "", "| # | Title | Views | Duration | Published |"])
            for i, v in enumerate(videos, 1):
                vtitle = v.get("title", "Untitled")
                vurl = f"https://youtu.be/{v.get('id', '')}"
                vviews = v.get("views")
                vdur = v.get("duration")
                vupload = v.get("upload_date", "")
                views_str = self._fmt_number(vviews) if vviews is not None else ""
                dur_str = self._fmt_duration(vdur) if vdur is not None else ""
                date_str = self._fmt_date(vupload) if vupload else ""
                lines.append(f"| {i} | [{vtitle}]({vurl}) | {views_str} | {dur_str} | {date_str} |")

        return "\n".join(lines)

    def _fmt_playlist(self, data: dict) -> str:
        pl = data.get("playlist", {})
        videos = data.get("videos", [])
        title = pl.get("title", "")
        channel = pl.get("channel", "")
        vcount = pl.get("video_count")
        pl_id = pl.get("id", "")
        url = f"https://youtube.com/playlist?list={pl_id}" if pl_id else ""

        lines = [f"## {title}"]
        if channel:
            lines.append(f"- **Channel:** {channel}")
        if vcount is not None:
            lines.append(f"- **Videos:** {vcount}")
        if url:
            lines.append(f"- **URL:** {url}")

        if videos:
            lines.extend(["", "### Videos", "", "| # | Title | Views | Duration | Published |"])
            for i, v in enumerate(videos, 1):
                vtitle = v.get("title", "Untitled")
                vurl = f"https://youtu.be/{v.get('id', '')}"
                vviews = v.get("views")
                vdur = v.get("duration")
                vupload = v.get("upload_date", "")
                views_str = self._fmt_number(vviews) if vviews is not None else ""
                dur_str = self._fmt_duration(vdur) if vdur is not None else ""
                date_str = self._fmt_date(vupload) if vupload else ""
                lines.append(f"| {i} | [{vtitle}]({vurl}) | {views_str} | {dur_str} | {date_str} |")

        return "\n".join(lines)

    def _fmt_transcript(self, data: dict) -> str:
        fragments = data.get("transcript", [])
        lines = ["## Transcript", "", "| Time | Text |"]
        for f in fragments:
            start = f.get("start", 0)
            text = f.get("text", "")
            m, s = divmod(int(start), 60)
            lines.append(f"| {m}:{s:02d} | {text} |")
        return "\n".join(lines)

    def _fmt_error(self, error_code: str, detail: str) -> str:
        return f"**Error:** {error_code}\n{detail}"

    # ------------------------------------------------------------------ #
    # Rich UI embed (HTMLResponse)
    # ------------------------------------------------------------------ #
    #
    # action="view" returns a bare HTMLResponse (not a tuple). Open WebUI's
    # middleware detects it, emits the `embeds` event via Socket.IO, and the
    # frontend renders it inline as a sandboxed iframe. The LLM never sees
    # the HTML and receives only the middleware's generic message.
    #
    # YouTube fast-path: the embed iframe only needs the 11-character video
    # ID, so view does NOT call the YT DLP API (GET /video) — YouTube
    # blocks automated metadata extraction with "login to confirm you're
    # not a bot", but the official youtube.com/embed/<id> iframe works
    # regardless. The ID is extracted directly from the argument (bare ID
    # or full watch/embed URL) and the embed is built locally, exactly like
    # the Open WebUI video embedder's YouTube fast-path.
    #
    # Sizing (same sandbox constraints as any Open WebUI embed):
    #  - `vh`/`vw` inside the sandboxed iframe refer to the iframe box
    #    (~150px initial), NOT the browser viewport. Any viewport cap is
    #    expressed via `screen.availHeight` (readable in the sandbox): the
    #    height never exceeds 65% of the available screen height.
    #  - The width derives from the chat container width and the video's
    #    aspect ratio. YouTube embeds are 16/9 by design.
    #  - `reportHeight()` posts the document's own height so the iframe
    #    hugs the content instead of staying at the tiny default box.
    #
    # Reimplemented from scratch for this tool (the author owns both this
    # repo and the Open WebUI video embedder; no credit requested).

    @staticmethod
    def _extract_video_id(value: str) -> Optional[str]:
        """Return the 11-char YouTube video ID from a bare ID or a YouTube URL."""
        if not value:
            return None
        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
            return value
        m = re.search(
            r"(?:youtube\.com|youtu\.be)/(?:watch\?v=|shorts/|embed/|live/|v/)?([A-Za-z0-9_-]{11})",
            value,
        )
        return m.group(1) if m else None

    @staticmethod
    def _build_embed_document(embed_url: str, title: str = "") -> str:
        """Build a self-contained HTML document embedding a YouTube iframe."""
        src = _html.escape(embed_url, quote=True)
        safe_title = _html.escape(title, quote=True)
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{color-scheme:light dark}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;overflow:hidden;background:transparent}}
body{{display:flex;align-items:center;justify-content:center;padding:16px}}
#player{{position:relative;width:100%;max-width:100%;border-radius:12px;overflow:hidden;background:#000;aspect-ratio:16/9}}
#player iframe{{position:absolute;inset:0;width:100%;height:100%;border:0}}
</style>
</head>
<body>
<div id="player">
<iframe src="{src}" title="{safe_title}" allow="autoplay;fullscreen" allowfullscreen loading="lazy"></iframe>
</div>
<script>
(function(){{
  var player=document.getElementById('player');
  function reportHeight(){{
    parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*');
  }}
  function fit(){{
    var maxH=(screen.availHeight||screen.height||0)*0.65;
    var cw=document.documentElement.clientWidth;
    var w=cw;
    if(maxH>0){{var wByH=maxH*16/9;if(wByH>0&&wByH<w)w=wByH;}}
    player.style.width=w+'px';
    player.style.height=(w*9/16)+'px';
    reportHeight();
  }}
  window.addEventListener('load',fit);
  addEventListener('resize',fit);
  new ResizeObserver(fit).observe(document.body);
  fit();
}})();
</script>
</body>
</html>
"""

    # ------------------------------------------------------------------ #
    # Public tool method
    # ------------------------------------------------------------------ #

    async def youtube_tool(
        self,
        action: str,
        type: str = "video",
        query: str = "",
        id: str = "",
        max_results: Optional[int] = None,
        sort: str = "relevance",
        language: Optional[str] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Query YouTube via the YT DLP API. action (verb) + type (resource) determine the call:

        - search: find content by keyword. Needs query. type=video (supports sort) | channel (returns @handle) | playlist (returns id)
        - get: fetch details for one item. Needs id. type=video (likes, upload date, tags, description) | transcript (timed fragments, supports language)
        - list: enumerate videos from a known resource. Needs id (channel @handle/handle/UCID or playlist id). Does not search
        - view: embed a video inline in the chat as a Rich UI player. Builds the YouTube iframe directly from id — NO API call, so it works even when YouTube blocks automated extraction. Terminal result: the player renders in the chat and the LLM sees only the middleware's generic message

        :param action: search | get | list | view
        :param type: video (default), channel, playlist, transcript
        :param query: Search term (required for search)
        :param id: Identifier of the resource, matching type: 11-char video ID or watch/embed URL (get+video, get+transcript, view), channel @handle/handle/UCID (list+channel), or playlist ID (list+playlist)
        :param max_results: Max results; omitted = UserValve default, clamped by UserValve and AdminValve caps
        :param sort: search+video: relevance (default), views, duration. list+channel: views (default), date, duration
        :param language: Transcript language (get+transcript only); omitted = UserValve preferred_language
        :param __event_emitter__: Injected by Open WebUI for status events
        """
        # --- status labels ---
        status_map = {
            ("search", "video"): "Searching videos...",
            ("search", "channel"): "Searching channels...",
            ("search", "playlist"): "Searching playlists...",
            ("get", "video"): "Fetching video metadata...",
            ("get", "transcript"): "Fetching transcript...",
            ("list", "channel"): "Fetching channel videos...",
            ("list", "playlist"): "Fetching playlist videos...",
            ("view", "video"): "Embedding video...",
        }
        label = status_map.get((action, type), "Processing...")
        await self._emit_status(__event_emitter__, label)

        # --- view action: build the embed directly from the video ID ---
        # YouTube blocks automated metadata extraction ("login to confirm
        # you're not a bot"), but the embed iframe only needs the
        # 11-character video ID. No YT DLP API call: extract the ID from
        # the argument (bare ID or URL) and build the iframe locally.
        if action == "view":
            if type == "video":
                embed_id = self._extract_video_id(id)
                if not embed_id:
                    await self._emit_status(__event_emitter__, label, done=True)
                    return self._fmt_error(
                        "invalid_video_id",
                        "view needs an 11-character YouTube video ID "
                        "(or a watch/embed URL) in id",
                    )
                embed_url = f"https://www.youtube.com/embed/{embed_id}?rel=0"
                document = self._build_embed_document(embed_url, title="YouTube video")
                await self._emit_status(__event_emitter__, label, done=True)
                return HTMLResponse(
                    content=document,
                    headers={"Content-Disposition": "inline"},
                )
            await self._emit_status(__event_emitter__, label, done=True)
            return self._fmt_error(
                "invalid_action", f"{action}/{type}: view only supports type='video'"
            )

        # --- build request ---
        try:
            url, params = self._build_request(
                action=action,
                type=type,
                query=query,
                id=id,
                max_results=max_results,
                sort=sort,
                language=language,
            )
        except ValueError as e:
            await self._emit_status(__event_emitter__, label, done=True)
            return self._fmt_error("invalid_action", f"{action}/{type}: {e}")

        # --- call API ---
        data = await self._call_api(url, params, __event_emitter__)
        await self._emit_status(__event_emitter__, label, done=True)

        # --- handle API error ---
        if "error" in data:
            if action == "list" and type == "channel" and data["error"] in (
                "channel_not_found", "channel_failed"
            ):
                return self._fmt_error(
                    data["error"],
                    "Use search with type='channel' to find the exact @handle first",
                )
            return self._fmt_error(data["error"], data.get("detail", ""))

        # --- format response ---
        if action == "search":
            results = data.get("results", [])
            if type == "playlist":
                return self._fmt_search_playlists(query, results)
            elif type == "channel":
                return self._fmt_search_channels(query, results)
            else:
                return self._fmt_search_videos(query, results)

        elif action == "get":
            if type == "video":
                return self._fmt_video(data)
            elif type == "transcript":
                return self._fmt_transcript(data)

        elif action == "list":
            if type == "channel":
                return self._fmt_channel(data)
            elif type == "playlist":
                return self._fmt_playlist(data)

        return self._fmt_error("unexpected", f"Unhandled action/type: {action}/{type}")
