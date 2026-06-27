import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

from silkworm import Request, run_spider
from silkworm.middlewares import (
    DelayMiddleware,
    RetryMiddleware,
    UserAgentMiddleware,
)
from silkworm.pipelines import JsonLinesPipeline

from tiktok_keyword_search import (
    DESKTOP_USER_AGENT,
    TikTokVideoSpider,
    _tikwm_search_url,
)


DEFAULT_OUTPUT = "data/tiktok_hashtag_videos.jl"
DEFAULT_TIKWM_DELAY = 1.25
DEFAULT_UKRAINIAN_HASHTAGS = (
    "україна",
    "украина",
    "новини",
    "новиниукраїни",
    "київ",
    "киев",
    "війна",
    "війнавукраїні",
    "зсу",
    "зсуукраїни",
    "славаукраїні",
    "українськамова",
    "харків",
    "одеса",
    "дніпро",
    "львів",
    "запоріжжя",
    "херсон",
    "миколаїв",
    "донбас",
    "крим",
    "фронт",
    "ппо",
    "ракети",
    "дрони",
    "бпла",
    "тривога",
    "повітрянатривога",
    "енергетика",
    "світло",
)
UKRAINIAN_TAG_RE = re.compile(r"[а-яА-ЯіїєґІЇЄҐ]")


class TikTokHashtagSpider(TikTokVideoSpider):
    name = "tiktok_hashtag_videos"

    async def start_requests(self):
        for query in self.queries:
            yield Request(
                url=_tikwm_search_url(query, self.max_videos_per_query or 25),
                callback=self.parse_tikwm_search,
                meta={"query": query, "tikwm_retries": 0},
            )


def _clean_hashtag(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    tag = value.strip().lstrip("#").strip()
    return tag or None


def _looks_ukrainian_tag(tag: str) -> bool:
    return bool(UKRAINIAN_TAG_RE.search(tag))


def extract_hashtag_queries(path: str | Path, *, ukrainian_only: bool = True) -> list[str]:
    queries: OrderedDict[str, str] = OrderedDict()

    with Path(path).open(encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON on line {line_number} of {path}: {exc}"
                ) from exc

            if not isinstance(item, dict):
                continue

            hashtags = item.get("hashtags")
            if not isinstance(hashtags, list):
                continue

            for value in hashtags:
                tag = _clean_hashtag(value)
                if tag is None:
                    continue
                if ukrainian_only and not _looks_ukrainian_tag(tag):
                    continue
                queries.setdefault(tag.casefold(), tag)

    return list(queries.values())


def _dedupe_tags(values: list[str] | tuple[str, ...]) -> list[str]:
    queries: OrderedDict[str, str] = OrderedDict()
    for value in values:
        tag = _clean_hashtag(value)
        if tag is None:
            continue
        queries.setdefault(tag.casefold(), tag)
    return list(queries.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read TikTok JSONL output, extract hashtags, and search TikTok "
            "for more videos using those hashtags."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help=(
            "Optional input JSON Lines file to extract hashtags from. "
            "If omitted, searches a built-in Ukrainian hashtag set."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"JSON Lines output path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print extracted hashtags and exit without scraping.",
    )
    parser.add_argument(
        "--ukrainian-tags",
        action="store_true",
        help="Use the built-in Ukrainian hashtag set even when an input file is provided.",
    )
    parser.add_argument(
        "--all-file-tags",
        action="store_true",
        help="When reading an input file, search all extracted hashtags instead of only Ukrainian-looking tags.",
    )
    parser.add_argument(
        "--tag",
        dest="extra_tags",
        action="append",
        default=[],
        help="Additional hashtag to search. Can be passed more than once.",
    )
    parser.add_argument(
        "--max-tags",
        type=int,
        default=None,
        help="Optional cap on number of extracted hashtags to search.",
    )
    parser.add_argument(
        "--max-videos-per-tag",
        type=int,
        default=25,
        help="Maximum discovered videos to follow from each hashtag search. Defaults to 25.",
    )
    parser.add_argument(
        "--backend",
        choices=("tikwm", "full"),
        default="tikwm",
        help=(
            "Search backend. 'tikwm' is direct and avoids TikTok/DDG discovery stalls; "
            "'full' uses the same cascade as tiktok_keyword_search.py. Defaults to tikwm."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent requests. Defaults to 1 to respect Tikwm rate limits.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_TIKWM_DELAY,
        help=f"Delay between requests in seconds. Defaults to {DEFAULT_TIKWM_DELAY}.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries: list[str] = []
    if args.input:
        queries.extend(
            extract_hashtag_queries(args.input, ukrainian_only=not args.all_file_tags)
        )
    if not args.input or args.ukrainian_tags:
        queries.extend(DEFAULT_UKRAINIAN_HASHTAGS)
    queries.extend(args.extra_tags)
    queries = _dedupe_tags(queries)

    if args.max_tags is not None:
        queries = queries[: args.max_tags]

    if not queries:
        raise SystemExit("No hashtags found.")

    if args.dry_run:
        for query in queries:
            print(query)
        return

    spider_cls = TikTokHashtagSpider if args.backend == "tikwm" else TikTokVideoSpider

    run_spider(
        spider_cls,
        queries=queries,
        max_videos_per_query=args.max_videos_per_tag,
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
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
