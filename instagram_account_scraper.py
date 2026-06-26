import argparse
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

from instagram_reels_search import (
    INSTAGRAM_APP_ID,
    INSTAGRAM_USER_AGENT,
    InstagramReelsSpider,
    _clean_reel_url,
    _extract_instagram_reel_urls,
    _iter_reel_items,
    _normalize_reel,
)


DEFAULT_ACCOUNTS = ()


def _read_lines_from_stdin() -> list[str]:
    if sys.stdin.isatty():
        return []
    return [line.strip() for line in sys.stdin if line.strip() and not line.startswith("#")]


def _normalize_account(value: str) -> str:
    value = value.strip()
    if not value:
        return value

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        value = parsed.path.strip("/").split("/", 1)[0]

    return value.removeprefix("@").strip("/")


def _profile_url(account: str) -> str:
    return f"https://www.instagram.com/{account}/"


def _profile_api_url(account: str) -> str:
    return f"https://www.instagram.com/api/v1/users/web_profile_info/?username={quote_plus(account)}"


def _account_seed_search_url(account: str) -> str:
    seed_query = f"site:instagram.com/reel/ {account}"
    return f"https://duckduckgo.com/html/?q={quote_plus(seed_query)}"


def _json_response(response: Response) -> dict[str, Any] | None:
    try:
        payload = json.loads(response.text)
    except (AttributeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class InstagramAccountSpider(InstagramReelsSpider):
    name = "instagram_account_reels"

    def __init__(
        self,
        *,
        accounts: list[str],
        max_reels_per_account: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(queries=[], reel_urls=[], max_reels_per_query=max_reels_per_account, **kwargs)
        self.accounts = [_normalize_account(account) for account in accounts]
        self.max_reels_per_account = max_reels_per_account
        self._seen_by_account: dict[str, set[str]] = {}
        self._yielded_by_account: dict[str, int] = {}

    async def start_requests(self):
        for account in self.accounts:
            if not account:
                continue
            yield Request(
                url=_profile_api_url(account),
                callback=self.parse_profile_api,
                headers={
                    "referer": _profile_url(account),
                    "x-ig-app-id": INSTAGRAM_APP_ID,
                    "user-agent": INSTAGRAM_USER_AGENT,
                },
                meta={"account": account},
            )

    async def parse_profile_api(self, response: Response):
        account = str(response.request.meta.get("account") or "")
        payload = _json_response(response)
        if payload is None:
            self.log.warning(
                "Instagram profile API returned non-JSON response; trying seed search",
                account=account,
                url=response.url,
            )
            yield self._seed_search_request(account)
            return

        user = payload.get("data", {}).get("user")
        if not isinstance(user, dict):
            self.log.warning(
                "Instagram profile API did not return a user; trying seed search",
                account=account,
                url=response.url,
            )
            yield self._seed_search_request(account)
            return

        yielded = 0
        for item in _iter_reel_items(user):
            if self._account_limit_reached(account):
                break
            source_url = f"https://www.instagram.com/reel/{item.get('shortcode') or item.get('code')}/"
            reel = _normalize_reel(item, source_url)
            if self._claim_reel(account, reel.get("id"), reel.get("source_url")):
                yielded += 1
                yield reel

        if yielded == 0 and not self._account_limit_reached(account):
            self.log.warning(
                "No Instagram reel nodes found in profile API; trying seed search",
                account=account,
                url=response.url,
            )
            yield self._seed_search_request(account)

    async def parse_account_seed_search(self, response: Response):
        if not isinstance(response, HTMLResponse):
            return

        account = str(response.request.meta.get("account") or "")
        yielded = 0
        for url in _extract_instagram_reel_urls(response.text):
            if self._account_limit_reached(account):
                break
            url = _clean_reel_url(url)
            if not self._claim_reel(account, None, url):
                continue
            yielded += 1
            yield Request(
                url=url,
                callback=self.parse_reel,
                meta={"account": account},
                headers={"user-agent": INSTAGRAM_USER_AGENT},
            )

        if yielded == 0:
            self.log.warning("No Instagram reel links found on account seed search", account=account, url=response.url)

    async def parse_reel(self, response: Response):
        account = response.request.meta.get("account")
        async for item in super().parse_reel(response):
            if account:
                item["source_account"] = account
            yield item

    def _seed_search_request(self, account: str) -> Request:
        return Request(
            url=_account_seed_search_url(account),
            callback=self.parse_account_seed_search,
            meta={"account": account},
        )

    def _claim_reel(self, account: str, reel_id: Any, source_url: Any) -> bool:
        marker = str(reel_id or source_url or "")
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
        if self.max_reels_per_account is None:
            return False
        return self._yielded_by_account.get(account, 0) >= self.max_reels_per_account


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape public Instagram Reel metadata from one or more Instagram accounts."
    )
    parser.add_argument(
        "accounts",
        nargs="*",
        help="Instagram accounts as usernames, @handles, or profile URLs.",
    )
    parser.add_argument(
        "--account",
        dest="extra_accounts",
        action="append",
        default=[],
        help="Instagram account to scrape. Can be passed more than once.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/instagram_account_reels.jl",
        help="JSON Lines output path. Defaults to data/instagram_account_reels.jl.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Number of concurrent requests. Defaults to 2.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between requests in seconds. Defaults to 1.0.",
    )
    parser.add_argument(
        "--max-reels-per-account",
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
        raise SystemExit("Provide at least one Instagram account, @handle, or profile URL.")

    run_spider(
        InstagramAccountSpider,
        accounts=accounts,
        max_reels_per_account=args.max_reels_per_account,
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
