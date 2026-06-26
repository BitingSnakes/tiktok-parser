# Shorts Scraper

Scrape public TikTok video metadata into JSON Lines files.

## Setup

Install dependencies:

```bash
uv sync
```

Run scripts with:

```bash
uv run python <script-name>.py
```

## Scrape TikTok Search Keywords

Scrape the default keyword set:

```bash
uv run python tiktok_keyword_search.py
```

Scrape specific keywords:

```bash
uv run python tiktok_keyword_search.py "україна" "українські новини"
```

Write to a custom output file:

```bash
uv run python tiktok_keyword_search.py "ukraine news" -o data/tiktok_videos.jl
```

Limit discovered videos per keyword:

```bash
uv run python tiktok_keyword_search.py "ukraine news" --max-videos-per-query 10
```

Scrape direct TikTok video URLs:

```bash
uv run python tiktok_keyword_search.py --url "https://www.tiktok.com/@tiktok/video/7655821093684448542"
```

Read keywords or URLs from stdin:

```bash
printf '%s\n' "ukraine news" "https://www.tiktok.com/@tiktok/video/7655821093684448542" \
  | uv run python tiktok_keyword_search.py
```

## Scrape TikTok Accounts

Scrape all videos from one account:

```bash
uv run python tiktok_account_scraper.py tiktok
```

Scrape multiple accounts:

```bash
uv run python tiktok_account_scraper.py tiktok nba washingtonpost
```

Handles and profile URLs are also accepted:

```bash
uv run python tiktok_account_scraper.py @tiktok "https://www.tiktok.com/@nba"
```

Write to a custom output file:

```bash
uv run python tiktok_account_scraper.py tiktok -o data/tiktok_account_videos.jl
```

Limit videos per account:

```bash
uv run python tiktok_account_scraper.py tiktok nba --max-videos-per-account 100
```

Read accounts from stdin:

```bash
printf '%s\n' tiktok nba washingtonpost \
  | uv run python tiktok_account_scraper.py
```

## Useful Options

Use lower concurrency and more delay if TikTok starts rate limiting:

```bash
uv run python tiktok_account_scraper.py tiktok \
  --concurrency 2 \
  --delay 2
```

Change account pagination size:

```bash
uv run python tiktok_account_scraper.py tiktok --page-size 20
```

Show all options:

```bash
uv run python tiktok_keyword_search.py --help
uv run python tiktok_account_scraper.py --help
```

## Search From Existing Hashtags

Extract hashtags from an existing JSON Lines scrape and use them as TikTok search queries:

```bash
uv run python tiktok_hashtag_search.py data/tiktok-two.jl
```

Preview extracted hashtags without scraping:

```bash
uv run python tiktok_hashtag_search.py data/tiktok-two.jl --dry-run
```

Limit how many hashtags are searched:

```bash
uv run python tiktok_hashtag_search.py data/tiktok-two.jl --max-tags 20
```

Limit videos per hashtag:

```bash
uv run python tiktok_hashtag_search.py data/tiktok-two.jl --max-videos-per-tag 10
```

Write hashtag search results to a custom output file:

```bash
uv run python tiktok_hashtag_search.py data/tiktok-two.jl -o data/tiktok_hashtag_videos.jl
```

## Output

Outputs are JSON Lines files. Each line is one video record with fields like:

```json
{
  "source_url": "https://www.tiktok.com/@tiktok/video/7655821093684448542",
  "scraped_at": "2026-06-26T22:39:44.057340+00:00",
  "id": "7655821093684448542",
  "description": "Video caption text",
  "created_at": 1782509772,
  "author": {},
  "stats": {},
  "video": {},
  "music": {},
  "hashtags": []
}
```

Default output paths:

```text
data/tiktok_videos.jl
data/tiktok_account_videos.jl
```
