import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from silkworm import HTMLResponse, Request, Response, Spider, run_spider
from silkworm.middlewares import (
    DelayMiddleware,
    RetryMiddleware,
    UserAgentMiddleware,
)
from silkworm.pipelines import JsonLinesPipeline


DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIDEO_ID_RE = re.compile(r"/video/(\d+)")
JSON_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
SCRIPT_ID_RE = re.compile(
    r'id=["\'](?P<id>__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE)["\']',
    re.IGNORECASE,
)
TIKTOK_VIDEO_HREF_RE = re.compile(
    r"https?://(?:www\.)?tiktok\.com/@[^\s\"'<>]*/video/\d+(?:\?[^\s\"'<>]*)?"
    r"|/@[^\s\"'<>]*/video/\d+(?:\?[^\s\"'<>]*)?"
)
DEFAULT_UKRAINIAN_QUERIES = (
    "україна",
    "українські новини",
    "українська мова",
    "українські пісні",
    "життя в україні",
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _compact(value: Any) -> Any:
    if value in ("", [], {}, None):
        return None
    return value


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_path(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _extract_video_id(url: str) -> str | None:
    if match := VIDEO_ID_RE.search(url):
        return match.group(1)
    return None


def _extract_json_blobs(html: str) -> list[tuple[str, dict[str, Any]]]:
    blobs: list[tuple[str, dict[str, Any]]] = []
    for match in JSON_SCRIPT_RE.finditer(html):
        id_match = SCRIPT_ID_RE.search(match.group("attrs"))
        if id_match is None:
            continue
        try:
            body = unescape(match.group("body")).strip()
            blobs.append((id_match.group("id"), json.loads(body)))
        except json.JSONDecodeError:
            continue
    return blobs


def _has_video_details(item: dict[str, Any]) -> bool:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    video = item.get("video") or {}
    return any(
        (
            item.get("desc"),
            _as_int(item.get("createTime")),
            _unique_id(author),
            stats.get("playCount"),
            stats.get("diggCount"),
            video.get("duration"),
            video.get("cover"),
            video.get("playAddr"),
        )
    )


def _find_item_struct(data: dict[str, Any], video_id: str | None) -> dict[str, Any] | None:
    item = _get_path(data, "__DEFAULT_SCOPE__", "webapp.video-detail", "itemInfo", "itemStruct")
    if isinstance(item, dict) and _has_video_details(item):
        return item

    item_module = data.get("ItemModule")
    if isinstance(item_module, dict):
        if (
            video_id
            and isinstance(item_module.get(video_id), dict)
            and _has_video_details(item_module[video_id])
        ):
            return item_module[video_id]
        for value in item_module.values():
            if isinstance(value, dict) and _has_video_details(value):
                return value

    for candidate in _walk(data):
        if (
            candidate.get("itemStruct")
            and isinstance(candidate["itemStruct"], dict)
            and _has_video_details(candidate["itemStruct"])
        ):
            return candidate["itemStruct"]
        if video_id and str(candidate.get("id")) == video_id and (
            "author" in candidate or "stats" in candidate or "video" in candidate
        ) and _has_video_details(candidate):
            return candidate

    return None


def _iter_item_structs(data: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    items: list[dict[str, Any]] = []

    for candidate in _walk(data):
        item = candidate.get("itemStruct")
        if isinstance(item, dict):
            marker = id(item)
            if marker not in seen and _has_video_details(item):
                seen.add(marker)
                items.append(item)
            continue

        if (
            candidate.get("id")
            and isinstance(candidate.get("author"), (dict, str))
            and ("stats" in candidate or "video" in candidate)
        ):
            marker = id(candidate)
            if marker not in seen and _has_video_details(candidate):
                seen.add(marker)
                items.append(candidate)

    return items


def _unique_id(author: Any) -> str | None:
    if isinstance(author, dict):
        return author.get("uniqueId") or author.get("id") or author.get("secUid")
    if isinstance(author, str):
        return author
    return None


def _hashtags(item: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for challenge in item.get("challenges") or []:
        if isinstance(challenge, dict) and challenge.get("title"):
            tags.append(str(challenge["title"]))
    return tags


def _video_url_from_item(item: dict[str, Any]) -> str | None:
    video_id = item.get("id")
    author = _unique_id(item.get("author"))
    if video_id and author:
        return f"https://www.tiktok.com/@{author}/video/{video_id}"
    return None


def _search_url(query: str) -> str:
    return f"https://www.tiktok.com/search/video?q={quote_plus(query)}"


def _seed_search_url(query: str) -> str:
    seed_query = f"site:tiktok.com/@ /video/ {query}"
    return f"https://duckduckgo.com/html/?q={quote_plus(seed_query)}"


def _tikwm_search_url(query: str, count: int) -> str:
    return f"https://www.tikwm.com/api/feed/search?keywords={quote_plus(query)}&count={count}&cursor=0"


def _absolute_tiktok_url(url: str) -> str:
    if url.startswith("/"):
        return f"https://www.tiktok.com{url}"
    return url


def _clean_video_url(url: str) -> str:
    parsed = urlparse(_absolute_tiktok_url(url))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _extract_tiktok_video_urls(text: str) -> list[str]:
    decoded = unescape(text)
    urls = [_clean_video_url(url) for url in TIKTOK_VIDEO_HREF_RE.findall(decoded)]

    for encoded in re.findall(r"uddg=([^&\"'<>]+)", decoded):
        target = unquote(encoded)
        if "tiktok.com" not in target:
            continue
        urls.extend(_clean_video_url(url) for url in TIKTOK_VIDEO_HREF_RE.findall(target))

    # DuckDuckGo sometimes entity-encodes the entire redirect URL.
    for href in re.findall(r"href=[\"']([^\"']+)[\"']", decoded):
        parsed = urlparse(unescape(href))
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target and "tiktok.com" in target:
            urls.extend(_clean_video_url(url) for url in TIKTOK_VIDEO_HREF_RE.findall(target))

    return urls


def _normalize_video(item: dict[str, Any], source_url: str) -> dict[str, Any]:
    author = item.get("author") or {}
    author_stats = item.get("authorStats") or {}
    stats = item.get("stats") or {}
    video = item.get("video") or {}
    music = item.get("music") or {}

    return {
        "source_url": source_url,
        "scraped_at": _utc_now(),
        "id": item.get("id") or _extract_video_id(source_url),
        "description": _compact(item.get("desc")),
        "created_at": _as_int(item.get("createTime")),
        "author": {
            "id": _compact(author.get("id") if isinstance(author, dict) else None),
            "unique_id": _compact(_unique_id(author)),
            "nickname": _compact(author.get("nickname") if isinstance(author, dict) else None),
            "verified": author.get("verified") if isinstance(author, dict) else None,
            "signature": _compact(author.get("signature") if isinstance(author, dict) else None),
            "followers": _as_int(author_stats.get("followerCount")),
            "following": _as_int(author_stats.get("followingCount")),
            "likes": _as_int(author_stats.get("heartCount")),
            "videos": _as_int(author_stats.get("videoCount")),
        },
        "stats": {
            "plays": _as_int(stats.get("playCount")),
            "likes": _as_int(stats.get("diggCount")),
            "comments": _as_int(stats.get("commentCount")),
            "shares": _as_int(stats.get("shareCount")),
            "saves": _as_int(stats.get("collectCount")),
        },
        "video": {
            "duration": _as_int(video.get("duration")),
            "ratio": _compact(video.get("ratio")),
            "cover": _compact(video.get("cover")),
            "dynamic_cover": _compact(video.get("dynamicCover")),
            "origin_cover": _compact(video.get("originCover")),
            "play_url": _compact(video.get("playAddr")),
            "download_url": _compact(video.get("downloadAddr")),
        },
        "music": {
            "id": _compact(music.get("id")),
            "title": _compact(music.get("title")),
            "author": _compact(music.get("authorName")),
            "original": music.get("original"),
            "play_url": _compact(music.get("playUrl")),
        },
        "hashtags": _hashtags(item),
    }


def _normalize_tikwm_video(item: dict[str, Any], query: str | None) -> dict[str, Any]:
    author = item.get("author") or {}
    music = item.get("music_info") or {}
    video_id = item.get("video_id")
    unique_id = author.get("unique_id") if isinstance(author, dict) else None
    source_url = (
        f"https://www.tiktok.com/@{unique_id}/video/{video_id}"
        if unique_id and video_id
        else f"https://www.tiktok.com/@/video/{video_id}"
    )

    result = {
        "source_url": source_url,
        "scraped_at": _utc_now(),
        "id": video_id,
        "description": _compact(item.get("title")),
        "created_at": _as_int(item.get("create_time")),
        "author": {
            "id": _compact(author.get("id") if isinstance(author, dict) else None),
            "unique_id": _compact(unique_id),
            "nickname": _compact(author.get("nickname") if isinstance(author, dict) else None),
            "verified": None,
            "signature": None,
            "followers": None,
            "following": None,
            "likes": None,
            "videos": None,
        },
        "stats": {
            "plays": _as_int(item.get("play_count")),
            "likes": _as_int(item.get("digg_count")),
            "comments": _as_int(item.get("comment_count")),
            "shares": _as_int(item.get("share_count")),
            "saves": _as_int(item.get("collect_count")),
        },
        "video": {
            "duration": _as_int(item.get("duration")),
            "ratio": None,
            "cover": _compact(item.get("cover")),
            "dynamic_cover": _compact(item.get("ai_dynamic_cover")),
            "origin_cover": _compact(item.get("origin_cover")),
            "play_url": _compact(item.get("play")),
            "download_url": _compact(item.get("wmplay")),
        },
        "music": {
            "id": _compact(music.get("id") if isinstance(music, dict) else item.get("music")),
            "title": _compact(music.get("title") if isinstance(music, dict) else None),
            "author": _compact(music.get("author") if isinstance(music, dict) else None),
            "original": music.get("original") if isinstance(music, dict) else None,
            "play_url": _compact(music.get("play") if isinstance(music, dict) else None),
        },
        "hashtags": re.findall(r"#([\wа-яА-ЯіїєґІЇЄҐ]+)", item.get("title") or ""),
        "metadata_source": "tikwm",
    }
    if query:
        result["search_query"] = query
    return result


async def _meta_content(response: HTMLResponse, selector: str) -> str | None:
    if element := await response.select_first(selector):
        return _compact(element.attr("content"))
    return None


class TikTokVideoSpider(Spider):
    name = "tiktok_videos"

    def __init__(
        self,
        *,
        queries: list[str] | None = None,
        video_urls: list[str] | None = None,
        max_videos_per_query: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.queries = queries or []
        self.video_urls = video_urls or []
        self.max_videos_per_query = max_videos_per_query

    async def start_requests(self):
        for query in self.queries:
            yield Request(
                url=_search_url(query),
                callback=self.parse_search,
                meta={"query": query},
            )
        for url in self.video_urls:
            yield Request(url=url, callback=self.parse_video)

    async def parse(self, response: Response):
        async for item in self.parse_video(response):
            yield item

    async def parse_search(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        query = response.request.meta.get("query")
        found: list[str] = []

        for _, blob in _extract_json_blobs(response.text):
            for item in _iter_item_structs(blob):
                if url := _video_url_from_item(item):
                    found.append(url)

        found.extend(_extract_tiktok_video_urls(response.text))

        yielded = 0
        async for request in self._video_requests(found, query=query):
            yielded += 1
            yield request

        if yielded == 0:
            self.log.warning(
                "No TikTok video links found on TikTok search page; trying seed search",
                query=query,
                url=response.url,
            )
            yield Request(
                url=_seed_search_url(str(query)),
                callback=self.parse_seed_search,
                meta={"query": query},
            )

    async def parse_seed_search(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        query = response.request.meta.get("query")
        yielded = 0
        async for request in self._video_requests(
            _extract_tiktok_video_urls(response.text),
            query=query,
        ):
            yielded += 1
            yield request

        if yielded == 0:
            self.log.warning("No TikTok video links found on seed search page", query=query, url=response.url)
            yield Request(
                url=_tikwm_search_url(
                    str(query),
                    self.max_videos_per_query or 25,
                ),
                callback=self.parse_tikwm_search,
                meta={"query": query, "tikwm_retries": 0},
            )

    async def parse_tikwm_search(self, response: Response):
        query = response.request.meta.get("query")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            self.log.warning("Tikwm fallback returned non-JSON response", query=query, url=response.url)
            return

        if payload.get("code") != 0:
            retries = int(response.request.meta.get("tikwm_retries") or 0)
            if "limit" in str(payload.get("msg", "")).lower() and retries < 3:
                await asyncio.sleep(1.25)
                yield Request(
                    url=response.url,
                    callback=self.parse_tikwm_search,
                    meta={"query": query, "tikwm_retries": retries + 1},
                    dont_filter=True,
                )
                return
            self.log.warning("Tikwm fallback did not return videos", query=query, message=payload.get("msg"))
            return

        videos = _get_path(payload, "data", "videos") or []
        if not videos:
            self.log.warning("Tikwm fallback returned no videos", query=query)
            return

        yielded = 0
        for video in videos:
            if not isinstance(video, dict):
                continue
            if self.max_videos_per_query is not None and yielded >= self.max_videos_per_query:
                break
            yielded += 1
            yield _normalize_tikwm_video(video, str(query) if query else None)

    async def _video_requests(self, urls: list[str], *, query: Any):
        yielded = 0
        seen: set[str] = set()
        for url in urls:
            url = _clean_video_url(url)
            if url in seen:
                continue
            seen.add(url)
            if self.max_videos_per_query is not None and yielded >= self.max_videos_per_query:
                break
            yielded += 1
            yield Request(url=url, callback=self.parse_video, meta={"query": query})

    async def parse_video(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        query = response.request.meta.get("query")
        video_id = _extract_video_id(response.url)
        for _, blob in _extract_json_blobs(response.text):
            if item := _find_item_struct(blob, video_id):
                video = _normalize_video(item, response.url)
                if query:
                    video["search_query"] = query
                yield video
                return

        item = {
            "source_url": response.url,
            "scraped_at": _utc_now(),
            "id": video_id,
            "title": await _meta_content(response, 'meta[property="og:title"]'),
            "description": await _meta_content(response, 'meta[property="og:description"]'),
            "image": await _meta_content(response, 'meta[property="og:image"]'),
            "canonical_url": (
                link.attr("href")
                if (link := await response.select_first('link[rel="canonical"]'))
                else None
            ),
            "warning": "No TikTok video JSON found; saved Open Graph fallback fields.",
        }
        if query:
            item["search_query"] = query
        yield item


def _read_lines_from_stdin() -> list[str]:
    if sys.stdin.isatty():
        return []
    return [line.strip() for line in sys.stdin if line.strip() and not line.startswith("#")]


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape public TikTok video metadata from TikTok search pages."
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help="TikTok search queries. Defaults to a small Ukrainian query set.",
    )
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        default=[],
        help="Direct TikTok video URL to scrape. Can be passed more than once.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/tiktok_videos.jl",
        help="JSON Lines output path. Defaults to data/tiktok_videos.jl.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of concurrent requests. Defaults to 4.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between requests in seconds. Defaults to 1.0.",
    )
    parser.add_argument(
        "--max-videos-per-query",
        type=int,
        default=25,
        help="Maximum discovered videos to follow from each search page. Defaults to 25.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inputs = [*args.queries, *_read_lines_from_stdin()]
    positional_urls = [value for value in inputs if _looks_like_url(value)]
    queries = [value for value in inputs if not _looks_like_url(value)]
    if not queries and not positional_urls and not args.urls:
        queries = list(DEFAULT_UKRAINIAN_QUERIES)

    run_spider(
        TikTokVideoSpider,
        queries=queries,
        video_urls=[*positional_urls, *args.urls],
        max_videos_per_query=args.max_videos_per_query,
        request_middlewares=[
            UserAgentMiddleware(default=DESKTOP_USER_AGENT),
            DelayMiddleware(delay=args.delay),
        ],
        response_middlewares=[
            RetryMiddleware(max_times=3, sleep_http_codes=[429, 503]),
        ],
        item_pipelines=[JsonLinesPipeline(args.output)],
        concurrency=args.concurrency,
        request_timeout=20,
        log_stats_interval=30,
    )


if __name__ == "__main__":
    main()
