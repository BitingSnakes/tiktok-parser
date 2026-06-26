import argparse
import asyncio
import json
import sys
from typing import Any
from urllib.parse import quote_plus, urlparse

from silkworm import HTMLResponse, Request, Response, Spider, run_spider
from silkworm.middlewares import (
    DelayMiddleware,
    RetryMiddleware,
    UserAgentMiddleware,
)
from silkworm.pipelines import JsonLinesPipeline

from tiktok_keyword_search import (
    DESKTOP_USER_AGENT,
    _as_int,
    _extract_json_blobs,
    _get_path,
    _iter_item_structs,
    _normalize_tikwm_video,
    _normalize_video,
    _video_url_from_item,
    _walk,
)


DEFAULT_ACCOUNTS = ()
DEFAULT_PAGE_SIZE = 35
MAX_EMPTY_PAGES = 2


def _read_lines_from_stdin() -> list[str]:
    if sys.stdin.isatty():
        return []
    return [
        line.strip() for line in sys.stdin if line.strip() and not line.startswith("#")
    ]


def _normalize_account(value: str) -> str:
    value = value.strip()
    if not value:
        return value

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        value = parsed.path.strip("/").split("/", 1)[0]

    return value.removeprefix("@").strip("/")


def _profile_url(account: str) -> str:
    return f"https://www.tiktok.com/@{account}"


def _tiktok_profile_feed_url(sec_uid: str, cursor: int, count: int) -> str:
    return (
        "https://www.tiktok.com/api/post/item_list/"
        f"?aid=1988&app_name=tiktok_web&device_platform=web_pc"
        f"&count={count}&cursor={cursor}&secUid={quote_plus(sec_uid)}"
    )


def _tikwm_user_posts_url(account: str, cursor: int, count: int) -> str:
    return (
        "https://www.tikwm.com/api/user/posts"
        f"?unique_id={quote_plus(account)}&count={count}&cursor={cursor}"
    )


def _find_sec_uid(data: dict[str, Any], account: str) -> str | None:
    user_module = data.get("UserModule")
    if isinstance(user_module, dict):
        users = user_module.get("users")
        if isinstance(users, dict):
            for key in (account, account.lower()):
                user = users.get(key)
                if isinstance(user, dict) and user.get("secUid"):
                    return str(user["secUid"])
            for user in users.values():
                if (
                    isinstance(user, dict)
                    and str(user.get("uniqueId", "")).lower() == account.lower()
                    and user.get("secUid")
                ):
                    return str(user["secUid"])

    for candidate in _walk(data):
        user = candidate.get("user")
        if isinstance(user, dict):
            if str(user.get("uniqueId", "")).lower() == account.lower() and user.get(
                "secUid"
            ):
                return str(user["secUid"])

        author = candidate.get("author")
        if isinstance(author, dict):
            if str(
                author.get("uniqueId", "")
            ).lower() == account.lower() and author.get("secUid"):
                return str(author["secUid"])

        if str(
            candidate.get("uniqueId", "")
        ).lower() == account.lower() and candidate.get("secUid"):
            return str(candidate["secUid"])

    return None


def _json_response(response: Response) -> dict[str, Any] | None:
    try:
        payload = json.loads(response.text)
    except AttributeError, json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _has_more(payload: dict[str, Any]) -> bool:
    return payload.get("hasMore") in (1, True, "1", "true", "True")


class TikTokAccountSpider(Spider):
    name = "tiktok_account_videos"

    def __init__(
        self,
        *,
        accounts: list[str],
        max_videos_per_account: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.accounts = [_normalize_account(account) for account in accounts]
        self.max_videos_per_account = max_videos_per_account
        self.page_size = page_size
        self._seen_by_account: dict[str, set[str]] = {}
        self._yielded_by_account: dict[str, int] = {}

    async def start_requests(self):
        for account in self.accounts:
            if not account:
                continue
            yield Request(
                url=_profile_url(account),
                callback=self.parse_profile,
                meta={"account": account},
            )

    async def parse(self, response: Response):
        async for item in self.parse_profile(response):
            yield item

    async def parse_profile(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        account = str(response.request.meta.get("account") or "")
        found_sec_uid: str | None = None
        yielded = 0

        for _, blob in _extract_json_blobs(response.text):
            found_sec_uid = found_sec_uid or _find_sec_uid(blob, account)
            for item in _iter_item_structs(blob):
                async for video in self._yield_tiktok_item(account, item, response.url):
                    yielded += 1
                    yield video

        if found_sec_uid and not self._account_limit_reached(account):
            yield Request(
                url=_tiktok_profile_feed_url(
                    found_sec_uid, cursor=0, count=self.page_size
                ),
                callback=self.parse_tiktok_feed,
                headers={"referer": _profile_url(account)},
                meta={
                    "account": account,
                    "sec_uid": found_sec_uid,
                    "cursor": 0,
                    "empty_pages": 0,
                },
            )
            return

        if yielded == 0:
            self.log.warning(
                "No TikTok profile video JSON found; trying Tikwm fallback",
                account=account,
                url=response.url,
            )
        else:
            self.log.warning(
                "No TikTok secUid found for pagination; trying Tikwm fallback",
                account=account,
                url=response.url,
            )

        if not self._account_limit_reached(account):
            yield Request(
                url=_tikwm_user_posts_url(account, cursor=0, count=self.page_size),
                callback=self.parse_tikwm_posts,
                meta={"account": account, "cursor": 0, "tikwm_retries": 0},
            )

    async def parse_tiktok_feed(self, response: Response):
        account = str(response.request.meta.get("account") or "")
        sec_uid = str(response.request.meta.get("sec_uid") or "")
        cursor = _as_int(response.request.meta.get("cursor")) or 0
        empty_pages = _as_int(response.request.meta.get("empty_pages")) or 0
        payload = _json_response(response)
        if payload is None:
            self.log.warning(
                "TikTok profile feed returned non-JSON response",
                account=account,
                url=response.url,
            )
            yield Request(
                url=_tikwm_user_posts_url(account, cursor=0, count=self.page_size),
                callback=self.parse_tikwm_posts,
                meta={"account": account, "cursor": 0, "tikwm_retries": 0},
            )
            return

        items = payload.get("itemList") or []
        yielded = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            async for video in self._yield_tiktok_item(account, item, response.url):
                yielded += 1
                yield video
                if self._account_limit_reached(account):
                    return

        if yielded == 0:
            empty_pages += 1
        else:
            empty_pages = 0

        next_cursor = _as_int(payload.get("cursor")) or 0
        if (
            _has_more(payload)
            and next_cursor
            and next_cursor != cursor
            and empty_pages < MAX_EMPTY_PAGES
            and not self._account_limit_reached(account)
        ):
            yield Request(
                url=_tiktok_profile_feed_url(
                    sec_uid, cursor=next_cursor, count=self.page_size
                ),
                callback=self.parse_tiktok_feed,
                headers={"referer": _profile_url(account)},
                meta={
                    "account": account,
                    "sec_uid": sec_uid,
                    "cursor": next_cursor,
                    "empty_pages": empty_pages,
                },
            )

    async def parse_tikwm_posts(self, response: Response):
        account = str(response.request.meta.get("account") or "")
        cursor = _as_int(response.request.meta.get("cursor")) or 0
        payload = _json_response(response)
        if payload is None:
            self.log.warning(
                "Tikwm fallback returned non-JSON response",
                account=account,
                url=response.url,
            )
            return

        if payload.get("code") != 0:
            retries = int(response.request.meta.get("tikwm_retries") or 0)
            if "limit" in str(payload.get("msg", "")).lower() and retries < 3:
                await asyncio.sleep(1.25)
                yield Request(
                    url=response.url,
                    callback=self.parse_tikwm_posts,
                    meta={
                        "account": account,
                        "cursor": cursor,
                        "tikwm_retries": retries + 1,
                    },
                    dont_filter=True,
                )
                return
            self.log.warning(
                "Tikwm fallback did not return videos",
                account=account,
                message=payload.get("msg"),
            )
            return

        videos = _get_path(payload, "data", "videos") or []
        yielded = 0
        saw_video = False
        for video in videos:
            if not isinstance(video, dict):
                continue
            saw_video = True
            normalized = _normalize_tikwm_video(video, None)
            if self._claim_video(
                account, normalized.get("id"), normalized.get("source_url")
            ):
                yielded += 1
                yield normalized
                if self._account_limit_reached(account):
                    return

        next_cursor = _as_int(_get_path(payload, "data", "cursor")) or 0
        has_more = _get_path(payload, "data", "hasMore") in (
            1,
            True,
            "1",
            "true",
            "True",
        )
        if has_more and next_cursor and next_cursor != cursor and saw_video:
            yield Request(
                url=_tikwm_user_posts_url(
                    account, cursor=next_cursor, count=self.page_size
                ),
                callback=self.parse_tikwm_posts,
                meta={"account": account, "cursor": next_cursor, "tikwm_retries": 0},
            )

    async def _yield_tiktok_item(
        self, account: str, item: dict[str, Any], source_url: str
    ):
        video = _normalize_video(item, _video_url_from_item(item) or source_url)
        if not self._claim_video(account, video.get("id"), video.get("source_url")):
            return
        yield video

    def _claim_video(self, account: str, video_id: Any, source_url: Any) -> bool:
        marker = str(video_id or source_url or "")
        if not marker:
            return False

        seen = self._seen_by_account.setdefault(account, set())
        if marker in seen:
            return False

        if self._account_limit_reached(account):
            return False

        seen.add(marker)
        self._yielded_by_account[account] = self._yielded_by_account.get(account, 0) + 1
        return True

    def _account_limit_reached(self, account: str) -> bool:
        if self.max_videos_per_account is None:
            return False
        return self._yielded_by_account.get(account, 0) >= self.max_videos_per_account


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape public TikTok video metadata from one or more TikTok accounts."
    )
    parser.add_argument(
        "accounts",
        nargs="*",
        help="TikTok accounts as usernames, @handles, or profile URLs.",
    )
    parser.add_argument(
        "--account",
        dest="extra_accounts",
        action="append",
        default=[],
        help="TikTok account to scrape. Can be passed more than once.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/tiktok_account_videos.jl",
        help="JSON Lines output path. Defaults to data/tiktok_account_videos.jl.",
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
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Videos requested per account page. Defaults to {DEFAULT_PAGE_SIZE}.",
    )
    parser.add_argument(
        "--max-videos-per-account",
        type=int,
        default=None,
        help="Optional cap per account. Defaults to no cap.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    accounts = [
        *args.accounts,
        *args.extra_accounts,
        *_read_lines_from_stdin(),
    ]
    accounts = [_normalize_account(account) for account in accounts]
    accounts = [account for account in accounts if account]
    if not accounts:
        accounts = list(DEFAULT_ACCOUNTS)

    if not accounts:
        raise SystemExit(
            "Provide at least one TikTok account, @handle, or profile URL."
        )

    run_spider(
        TikTokAccountSpider,
        accounts=accounts,
        max_videos_per_account=args.max_videos_per_account,
        page_size=args.page_size,
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
