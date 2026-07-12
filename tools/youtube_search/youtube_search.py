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

from typing import Optional
import httpx
from pydantic import BaseModel, Field


HARD_LIMIT = 50


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
            description="Hard limit on results. Cannot exceed 50.",
            ge=1,
            le=50,
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
                    "data": {"message": message, "type": level},
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
            HARD_LIMIT,
        )

    # ------------------------------------------------------------------ #
    # URL / params builder
    # ------------------------------------------------------------------ #

    def _build_request(self, action: str, **kwargs) -> tuple[str, dict]:
        base = self.valves.api_base_url.rstrip("/")

        if action == "search":
            params = {
                "query": kwargs["query"],
                "max_results": self._resolve_max_results(kwargs.get("max_results")),
                "type": kwargs.get("search_type", "video"),
            }
            if kwargs.get("sort") and kwargs["search_type"] in ("video", ""):
                params["sort"] = kwargs["sort"]
            return f"{base}/search", params

        elif action == "video":
            return f"{base}/video", {"video_id": kwargs["video_id"]}

        elif action == "channel":
            params = {
                "name": kwargs["channel_name"],
                "max_results": self._resolve_max_results(kwargs.get("max_results")),
            }
            if kwargs.get("sort"):
                params["sort"] = kwargs["sort"]
            return f"{base}/channel", params

        elif action == "playlist":
            params = {
                "id": kwargs["playlist_id"],
                "max_results": self._resolve_max_results(kwargs.get("max_results")),
            }
            return f"{base}/playlist", params

        elif action == "transcript":
            params = {"video_id": kwargs["video_id"]}
            lang = (
                kwargs.get("language")
                if kwargs.get("language") is not None
                else self.user_valves.preferred_language
            )
            if lang:
                params["language"] = lang
            return f"{base}/transcript", params

        elif action == "health":
            return f"{base}/health", {}

        raise ValueError(f"Unknown action: {action}")

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
        return f"{n:,}"

    @staticmethod
    def _fmt_date(yyyymmdd: str) -> str:
        if not yyyymmdd or len(yyyymmdd) != 8:
            return yyyymmdd
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"

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
        # Fallback: if no type is set, assume video by ID pattern
        if id_ and len(id_) == 11:
            return f"https://youtu.be/{id_}"
        return ""

    def _fmt_search_videos(self, query: str, results: list) -> str:
        lines = [f"## Search results for \"{query}\""]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = self._fmt_url(r)
            channel = r.get("channel", "")
            views = r.get("views")
            duration = r.get("duration")
            thumb = r.get("thumbnail", "")
            desc = r.get("description", "")

            lines.append(f"\n### {i}. [{title}]({url})")
            if channel:
                lines.append(f"- **Channel:** {channel}")
            if views is not None:
                lines.append(f"- **Views:** {self._fmt_number(views)}")
            if duration is not None:
                lines.append(f"- **Duration:** {self._fmt_duration(duration)}")
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
            subs = r.get("subscriber_count")
            thumb = r.get("thumbnail", "")

            lines.append(f"\n### {i}. [{title}]({url})")
            if subs:
                lines.append(f"- **Subscribers:** {subs}")
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
        if subs:
            lines.append(f"- **Subscribers:** {subs:,}" if isinstance(subs, int) else f"- **Subscribers:** {subs}")
        if url:
            lines.append(f"- **URL:** {url}")

        if videos:
            lines.extend(["", "### Videos", "", "| # | Title | Views | Duration |"])
            for i, v in enumerate(videos, 1):
                vtitle = v.get("title", "Untitled")
                vurl = f"https://youtu.be/{v.get('id', '')}"
                vviews = v.get("views")
                vdur = v.get("duration")
                views_str = self._fmt_number(vviews) if vviews is not None else ""
                dur_str = self._fmt_duration(vdur) if vdur is not None else ""
                lines.append(f"| {i} | [{vtitle}]({vurl}) | {views_str} | {dur_str} |")

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
            lines.extend(["", "### Videos", "", "| # | Title | Views | Duration |"])
            for i, v in enumerate(videos, 1):
                vtitle = v.get("title", "Untitled")
                vurl = f"https://youtu.be/{v.get('id', '')}"
                vviews = v.get("views")
                vdur = v.get("duration")
                views_str = self._fmt_number(vviews) if vviews is not None else ""
                dur_str = self._fmt_duration(vdur) if vdur is not None else ""
                lines.append(f"| {i} | [{vtitle}]({vurl}) | {views_str} | {dur_str} |")

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

    def _fmt_health(self, data: dict) -> str:
        lines = [
            "## Service Status",
            "",
            f"- **Status:** {data.get('status', 'unknown')}",
            f"- **yt-dlp version:** {data.get('yt_dlp_version', 'N/A')}",
            f"- **Deno version:** {data.get('deno_version', 'N/A')}",
            f"- **Proxy configured:** {'yes' if data.get('proxy_configured') else 'no'}",
            f"- **SSL verification:** {'yes' if data.get('ssl_verification') else 'no'}",
        ]
        return "\n".join(lines)

    def _fmt_error(self, error_code: str, detail: str) -> str:
        return f"**Error:** {error_code}\n{detail}"

    # ------------------------------------------------------------------ #
    # Public tool method
    # ------------------------------------------------------------------ #

    async def youtube_tool(
        self,
        action: str,
        query: str = "",
        video_id: str = "",
        channel_name: str = "",
        playlist_id: str = "",
        max_results: Optional[int] = None,
        sort: str = "relevance",
        search_type: str = "video",
        language: Optional[str] = None,
        __event_emitter__=None,
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
        # --- status labels ---
        status_labels = {
            "search": "Searching YouTube...",
            "video": "Fetching video metadata...",
            "channel": "Fetching channel info...",
            "playlist": "Fetching playlist info...",
            "transcript": "Fetching transcript...",
            "health": "Checking service status...",
        }

        await self._emit_status(
            __event_emitter__,
            status_labels.get(action, "Processing..."),
        )

        # --- build request ---
        try:
            url, params = self._build_request(
                action=action,
                query=query,
                video_id=video_id,
                channel_name=channel_name,
                playlist_id=playlist_id,
                max_results=max_results,
                sort=sort,
                search_type=search_type,
                language=language,
            )
        except ValueError as e:
            await self._emit_status(
                __event_emitter__, status_labels.get(action, "Processing..."), done=True
            )
            return self._fmt_error("invalid_action", str(e))

        # --- call API ---
        data = await self._call_api(url, params, __event_emitter__)

        await self._emit_status(
            __event_emitter__, status_labels.get(action, "Processing..."), done=True
        )

        # --- handle API error ---
        if "error" in data:
            return self._fmt_error(data["error"], data.get("detail", ""))

        # --- format response ---
        if action == "search":
            st = params.get("type", "video")
            results = data.get("results", [])
            if st == "playlist":
                return self._fmt_search_playlists(query, results)
            elif st == "channel":
                return self._fmt_search_channels(query, results)
            else:
                return self._fmt_search_videos(query, results)

        elif action == "video":
            return self._fmt_video(data)

        elif action == "channel":
            return self._fmt_channel(data)

        elif action == "playlist":
            return self._fmt_playlist(data)

        elif action == "transcript":
            return self._fmt_transcript(data)

        elif action == "health":
            return self._fmt_health(data)

        return self._fmt_error("unexpected", f"Unhandled action: {action}")
