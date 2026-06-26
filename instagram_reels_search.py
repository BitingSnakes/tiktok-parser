import argparse
import json
import re
import sys
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

from tiktok_keyword_search import _as_int, _compact, _utc_now


INSTAGRAM_APP_ID = "936619743392459"
INSTAGRAM_USER_AGENT = "Mozilla/5.0"
REEL_CODE_RE = re.compile(r"/reels?/([A-Za-z0-9_-]+)")
JSON_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
META_RE = re.compile(
    r"<meta(?P<attrs>[^>]*)>",
    re.DOTALL | re.IGNORECASE,
)
ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_:.-]+)=[\"'](?P<value>.*?)[\"']",
    re.DOTALL,
)
INSTAGRAM_REEL_HREF_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/reels?/[A-Za-z0-9_-]+/?(?:\?[^\s\"'<>]*)?"
    r"|/reels?/[A-Za-z0-9_-]+/?(?:\?[^\s\"'<>]*)?"
)
HASHTAG_RE = re.compile(r"#([\wа-яА-ЯіїєґІЇЄҐ]+)")
DEFAULT_UKRAINIAN_QUERIES = (
    "україна",
    "українські новини",
    "київ новини",
    "українська мова",
    "життя в україні",
)


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _extract_reel_code(url: str) -> str | None:
    if match := REEL_CODE_RE.search(url):
        return match.group(1)
    return None


def _search_url(query: str) -> str:
    seed_query = f"site:instagram.com/reel/ {query}"
    return f"https://r.jina.ai/http://duckduckgo.com/html/?q={quote_plus(seed_query)}"


def _duckduckgo_search_url(query: str) -> str:
    seed_query = f"site:instagram.com/reel/ {query}"
    return f"https://duckduckgo.com/html/?q={quote_plus(seed_query)}"


def _absolute_instagram_url(url: str) -> str:
    if url.startswith("/"):
        return f"https://www.instagram.com{url}"
    return url


def _clean_reel_url(url: str) -> str:
    parsed = urlparse(_absolute_instagram_url(url))
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}/"


def _extract_instagram_reel_urls(text: str) -> list[str]:
    decoded = unescape(text)
    urls = [_clean_reel_url(url) for url in INSTAGRAM_REEL_HREF_RE.findall(decoded)]

    for encoded in re.findall(r"uddg=([^&\"'<>]+)", decoded):
        target = unquote(encoded)
        if "instagram.com" not in target:
            continue
        urls.extend(_clean_reel_url(url) for url in INSTAGRAM_REEL_HREF_RE.findall(target))

    for href in re.findall(r"href=[\"']([^\"']+)[\"']", decoded):
        parsed = urlparse(unescape(href))
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target and "instagram.com" in target:
            urls.extend(_clean_reel_url(url) for url in INSTAGRAM_REEL_HREF_RE.findall(target))

    return urls


def _extract_json_blobs(html: str) -> list[Any]:
    blobs: list[Any] = []
    for match in JSON_SCRIPT_RE.finditer(html):
        attrs = match.group("attrs")
        body = unescape(match.group("body")).strip()
        if not body:
            continue

        if "application/ld+json" in attrs or body.startswith(("{", "[")):
            try:
                blobs.append(json.loads(body))
            except json.JSONDecodeError:
                continue

    return blobs


def _meta_tags(html: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for match in META_RE.finditer(html):
        attrs = {
            attr.group("name").lower(): unescape(attr.group("value"))
            for attr in ATTR_RE.finditer(match.group("attrs"))
        }
        key = attrs.get("property") or attrs.get("name")
        content = attrs.get("content")
        if key and content:
            tags[key] = content
    return tags


def _parse_compact_count(value: str) -> int | None:
    value = value.strip().replace(",", "")
    multiplier = 1
    if value[-1:].lower() == "k":
        multiplier = 1_000
        value = value[:-1]
    elif value[-1:].lower() == "m":
        multiplier = 1_000_000
        value = value[:-1]

    try:
        return int(float(value) * multiplier)
    except ValueError:
        return None


def _og_description_parts(description: str | None) -> tuple[int | None, int | None, str | None, str | None]:
    if not description:
        return None, None, None, None

    pattern = re.compile(
        r"(?P<likes>[\d,.]+[KkMm]?)\s+likes,\s+"
        r"(?P<comments>[\d,.]+[KkMm]?)\s+comments\s+-\s+"
        r"(?P<username>[\w.]+)\s+on\s+[^:]+:\s+\"(?P<caption>.*)\"\s*$",
        re.DOTALL,
    )
    match = pattern.search(description)
    if not match:
        pattern = re.compile(
            r"(?P<likes>[\d,.]+[KkMm]?)\s+likes,\s+"
            r"(?P<comments>[\d,.]+[KkMm]?)\s+comments\s+-\s+"
            r"(?P<username>[\w.]+)\s+on\s+[^:]+:\s+\"(?P<caption>.*)\"\.\s*$",
            re.DOTALL,
        )
        match = pattern.search(description)
    if not match:
        return None, None, None, description

    return (
        _parse_compact_count(match.group("likes")),
        _parse_compact_count(match.group("comments")),
        match.group("username"),
        match.group("caption"),
    )


def _nickname_from_title(title: str | None) -> str | None:
    if not title:
        return None
    if " on Instagram:" in title:
        return title.split(" on Instagram:", 1)[0]
    return title


def _first_caption(item: dict[str, Any]) -> str | None:
    if isinstance(item.get("caption"), str):
        return item["caption"]

    caption = item.get("caption")
    if isinstance(caption, dict) and isinstance(caption.get("text"), str):
        return caption["text"]

    edges = item.get("edge_media_to_caption", {}).get("edges")
    if isinstance(edges, list):
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict) and isinstance(node.get("text"), str):
                return node["text"]

    return None


def _owner(item: dict[str, Any]) -> dict[str, Any]:
    owner = item.get("owner") or item.get("user") or {}
    return owner if isinstance(owner, dict) else {}


def _count(value: Any) -> int | None:
    if isinstance(value, dict) and "count" in value:
        return _as_int(value.get("count"))
    return _as_int(value)


def _music_info(item: dict[str, Any]) -> dict[str, Any]:
    clips_metadata = item.get("clips_metadata") or {}
    if not isinstance(clips_metadata, dict):
        clips_metadata = {}

    music_info = clips_metadata.get("music_info") or {}
    if not isinstance(music_info, dict):
        music_info = {}

    music_asset = music_info.get("music_asset_info") or {}
    if not isinstance(music_asset, dict):
        music_asset = {}

    original = music_info.get("original_sound_info") or {}
    if not isinstance(original, dict):
        original = {}
    artist = original.get("ig_artist")
    original_artist = artist.get("username") if isinstance(artist, dict) else None

    return {
        "id": _compact(
            music_asset.get("audio_cluster_id")
            or music_asset.get("id")
            or original.get("audio_asset_id")
        ),
        "title": _compact(music_asset.get("title") or original.get("original_audio_title")),
        "author": _compact(music_asset.get("display_artist") or original_artist),
        "original": original.get("is_original_audio"),
        "play_url": _compact(music_asset.get("progressive_download_url")),
    }


def _is_reel_item(item: dict[str, Any], code: str | None = None) -> bool:
    shortcode = item.get("shortcode") or item.get("code")
    if code and shortcode and str(shortcode) != code:
        return False

    typename = str(item.get("__typename") or item.get("__type") or "")
    return any(
        (
            shortcode and item.get("is_video") is True,
            shortcode and item.get("video_url"),
            shortcode and "Video" in typename,
            item.get("@type") == "VideoObject",
        )
    )


def _find_reel_item(data: Any, code: str | None) -> dict[str, Any] | None:
    for candidate in _walk(data):
        if _is_reel_item(candidate, code):
            return candidate
    return None


def _iter_reel_items(data: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for candidate in _walk(data):
        if not _is_reel_item(candidate):
            continue
        marker = str(candidate.get("shortcode") or candidate.get("code") or id(candidate))
        if marker in seen:
            continue
        seen.add(marker)
        items.append(candidate)
    return items


def _normalize_reel(item: dict[str, Any], source_url: str) -> dict[str, Any]:
    owner = _owner(item)
    shortcode = item.get("shortcode") or item.get("code") or _extract_reel_code(source_url)
    description = _compact(_first_caption(item))

    result = {
        "source_url": source_url,
        "scraped_at": _utc_now(),
        "id": _compact(item.get("id") or shortcode),
        "description": description,
        "created_at": _as_int(item.get("taken_at_timestamp") or item.get("taken_at")),
        "author": {
            "id": _compact(owner.get("id")),
            "unique_id": _compact(owner.get("username")),
            "nickname": _compact(owner.get("full_name")),
            "verified": owner.get("is_verified"),
            "signature": _compact(owner.get("biography")),
            "followers": _count(owner.get("edge_followed_by") or owner.get("follower_count")),
            "following": _count(owner.get("edge_follow") or owner.get("following_count")),
            "likes": None,
            "videos": _count(owner.get("edge_owner_to_timeline_media") or owner.get("media_count")),
        },
        "stats": {
            "plays": _as_int(item.get("video_view_count") or item.get("play_count")),
            "likes": _count(item.get("edge_media_preview_like") or item.get("like_count")),
            "comments": _count(item.get("edge_media_to_comment") or item.get("comment_count")),
            "shares": None,
            "saves": None,
        },
        "video": {
            "duration": _as_int(item.get("video_duration")),
            "ratio": None,
            "cover": _compact(item.get("display_url") or item.get("thumbnail_src") or item.get("thumbnailUrl")),
            "dynamic_cover": None,
            "origin_cover": None,
            "play_url": _compact(item.get("video_url") or item.get("contentUrl")),
            "download_url": None,
        },
        "music": _music_info(item),
        "hashtags": HASHTAG_RE.findall(description or ""),
        "metadata_source": "instagram",
    }
    return result


def _normalize_og_reel(source_url: str, code: str | None, tags: dict[str, str]) -> dict[str, Any]:
    og_description = tags.get("og:description")
    likes, comments, username, caption = _og_description_parts(og_description)
    title = tags.get("og:title")
    canonical_url = tags.get("og:url") or source_url

    return {
        "source_url": canonical_url,
        "scraped_at": _utc_now(),
        "id": code,
        "description": _compact(caption),
        "created_at": None,
        "author": {
            "id": None,
            "unique_id": _compact(username),
            "nickname": _compact(_nickname_from_title(title)),
            "verified": None,
            "signature": None,
            "followers": None,
            "following": None,
            "likes": None,
            "videos": None,
        },
        "stats": {
            "plays": None,
            "likes": likes,
            "comments": comments,
            "shares": None,
            "saves": None,
        },
        "video": {
            "duration": None,
            "ratio": None,
            "cover": _compact(tags.get("og:image")),
            "dynamic_cover": None,
            "origin_cover": None,
            "play_url": _compact(tags.get("og:video") or tags.get("og:video:url")),
            "download_url": None,
        },
        "music": {
            "id": None,
            "title": None,
            "author": None,
            "original": None,
            "play_url": None,
        },
        "hashtags": HASHTAG_RE.findall(caption or og_description or ""),
        "metadata_source": "instagram_open_graph",
    }


async def _meta_content(response: HTMLResponse, selector: str) -> str | None:
    if element := await response.select_first(selector):
        return _compact(element.attr("content"))
    return None


class InstagramReelsSpider(Spider):
    name = "instagram_reels"

    def __init__(
        self,
        *,
        queries: list[str] | None = None,
        reel_urls: list[str] | None = None,
        max_reels_per_query: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.queries = queries or []
        self.reel_urls = reel_urls or []
        self.max_reels_per_query = max_reels_per_query

    async def start_requests(self):
        for query in self.queries:
            yield Request(
                url=_search_url(query),
                callback=self.parse_search,
                meta={"query": query},
                headers={"user-agent": INSTAGRAM_USER_AGENT},
            )
        for url in self.reel_urls:
            yield Request(
                url=_clean_reel_url(url),
                callback=self.parse_reel,
                headers={"user-agent": INSTAGRAM_USER_AGENT},
            )

    async def parse(self, response: Response):
        async for item in self.parse_reel(response):
            yield item

    async def parse_search(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        query = response.request.meta.get("query")
        tried_duckduckgo = response.request.meta.get("backend") == "duckduckgo"
        found = _extract_instagram_reel_urls(response.text)
        yielded = 0
        seen: set[str] = set()
        for url in found:
            if url in seen:
                continue
            seen.add(url)
            if self.max_reels_per_query is not None and yielded >= self.max_reels_per_query:
                break
            yielded += 1
            yield Request(
                url=url,
                callback=self.parse_reel,
                meta={"query": query},
                headers={"user-agent": INSTAGRAM_USER_AGENT},
            )

        if yielded == 0:
            if not tried_duckduckgo:
                self.log.warning(
                    "No Instagram reel links found on primary search page; trying DuckDuckGo",
                    query=query,
                    url=response.url,
                )
                yield Request(
                    url=_duckduckgo_search_url(str(query)),
                    callback=self.parse_search,
                    meta={"query": query, "backend": "duckduckgo"},
                    headers={"user-agent": INSTAGRAM_USER_AGENT},
                )
                return
            self.log.warning("No Instagram reel links found on seed search page", query=query, url=response.url)

    async def parse_reel(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        query = response.request.meta.get("query")
        code = _extract_reel_code(response.url)
        for blob in _extract_json_blobs(response.text):
            if item := _find_reel_item(blob, code):
                reel = _normalize_reel(item, response.url)
                if query:
                    reel["search_query"] = query
                yield reel
                return

        item = _normalize_og_reel(response.url, code, _meta_tags(response.text))
        item["warning"] = "No Instagram reel JSON found; saved Open Graph fallback fields."
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
        description="Scrape public Instagram Reel metadata from search queries or direct Reel URLs."
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help="Instagram Reel search queries. Defaults to a small Ukrainian query set.",
    )
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        default=[],
        help="Direct Instagram Reel URL to scrape. Can be passed more than once.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/instagram_reels.jl",
        help="JSON Lines output path. Defaults to data/instagram_reels.jl.",
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
        "--max-reels-per-query",
        type=int,
        default=25,
        help="Maximum discovered Reels to follow from each search page. Defaults to 25.",
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
        InstagramReelsSpider,
        queries=queries,
        reel_urls=[*positional_urls, *args.urls],
        max_reels_per_query=args.max_reels_per_query,
        request_middlewares=[
            UserAgentMiddleware(default=INSTAGRAM_USER_AGENT),
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
