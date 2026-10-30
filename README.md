# The Feed Filter

A personal AI agent that reads your feeds so you don't have to. It pulls from Hacker News, Reddit,
X (Twitter) and any RSS/Atom feed, scores every item 1-10 against a plain-markdown **Interest
Profile** using Claude, drops the clickbait, outrage and repeats, and writes a structured
**Daily Digest** you can read in the terminal, as Markdown, or in a local dashboard.

```
 providers ──▶ raw_items ──▶ LLM filter ──▶ filtered_items ──▶ digest ──▶ reports/ + dashboard
 (HN, Reddit,   (SQLite,      (1-10 score,    (score, theme,     (Markdown,
  X, RSS)        deduped)      takeaways,      takeaways,         grouped by
                               flags)          flags)             theme)
```

## Quick start

```bash
git clone <this repo> feed-filter && cd feed-filter
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # or: uv pip install -r requirements-dev.txt

cp .env.example .env                     # add ANTHROPIC_API_KEY (see "Adding API tokens")
# interest_profile.md and sources.toml ship with sensible defaults - edit to taste

python main.py --digest                  # collect -> filter -> summarize
python main.py --serve                   # dashboard at http://127.0.0.1:8000
```

No API keys yet? Run the whole pipeline offline with bundled fixture data and a heuristic scorer:

```bash
python main.py --digest --mock --llm mock
```

Hacker News and RSS need no credentials at all, so even without an `ANTHROPIC_API_KEY` you can
run `python main.py --collect` against live sources and inspect the database.

## CLI reference

| Command | What it does |
|---|---|
| `python main.py --digest` | Full pipeline: collect, filter, summarize. Writes `reports/YYYY-MM-DD.md` and stores the report. |
| `python main.py --collect` | Only ingest new items from the enabled providers (dedupes against the DB). |
| `python main.py --filter` | Only score items that have not yet been evaluated against the current profile. |
| `python main.py --summarize` | Only build a digest from already-scored items (`--date YYYY-MM-DD` to label it). |
| `python main.py --serve` | Start the read-only dashboard (`--host`, `--port`). |
| `python main.py --list-reports` | List stored digests. |
| `python main.py --show-report ID` | Print a digest as Markdown. |
| `python main.py --export-html` | Write a standalone static site of every digest to `reports/html/` (`--output DIR`). |
| `python main.py --init-db` | Create the schema and exit. |

Useful flags: `--mock` (fixture data instead of live providers), `--llm mock|anthropic`,
`--model claude-...`, `--profile PATH`, `--db URL`, `-v` for debug logs.

Scheduling: `scripts/run_daily.sh` activates the venv and runs `--digest`. A crontab line for
07:30 every morning:

```
30 7 * * * /path/to/feed-filter/scripts/run_daily.sh >> /path/to/feed-filter/data/cron.log 2>&1
```

## Project layout

```
main.py                       CLI entry point (python main.py --digest)
interest_profile.md           Your interest profile (edit this!)
sources.toml                  What to collect: feeds, subreddits, X accounts
src/feed_filter/
  config.py                   Settings from .env / environment + sources.toml
  cli.py                      argparse CLI
  providers/                  COLLECTION LAYER - one module per source
    base.py                   Provider contract, CollectedItem, URL normalisation, content hashing
    hackernews.py             Algolia HN API (no auth)
    reddit.py                 Public JSON or OAuth; auto token fetch from client credentials
    rss.py                    feedparser-based RSS/Atom
    x.py                      API v2 -> Nitter RSS -> syndication scrape (cookie), in that order
    mock.py + fixtures/       Fixture-backed provider used for tests, --mock and fallbacks
  db/                         DATABASE LAYER - SQLAlchemy 2 + SQLite
    models.py                 raw_items, filtered_items, interest_profiles, reports
    repository.py             All reads/writes; duplicate-safe ingestion
    session.py                Engine/session factory (WAL, foreign keys)
  services/
    profile.py                Parses interest_profile.md into themes, weights, keywords, settings
    llm.py                    AI ENGINE - Claude client with JSON-schema output, plus a mock scorer
    filter_engine.py          Batches items to the LLM, computes final scores, persists verdicts
    summarizer.py             SUMMARY GENERATION - selects, groups by theme, renders Markdown
    collector.py              Runs providers, tolerates failures, applies mock fallback
    pipeline.py               Orchestrates collect -> filter -> summarize
  web/
    app.py                    FastAPI dashboard + JSON API + static HTML exporter
    templates/                Jinja2 templates (dark/light aware, phone friendly)
tests/                        pytest suite (providers, DB, profile, engine, summarizer, web, CLI)
reports/                      Generated digests (Markdown; HTML export under reports/html/)
data/                         SQLite database
```

## The Interest Profile

`interest_profile.md` is both the prompt the model sees and the configuration for scoring. The
parser understands four sections; anything else is ignored, so you can keep notes in the file.

```markdown
# Interest Profile

## About Me
Free text sent to the model verbatim. Say who you are and *why* you read.

## Themes
### Tech Breakthroughs (weight: 1.0)
keywords: llm, postgres, rust, benchmark
Free-text description of what belongs here.

### Business Insights (weight: 0.9)
keywords: startup, pricing, growth
...

## Avoid
- outrage and culture-war framing
- gadget listicles

## Settings
min_score: 6
max_items_per_theme: 5
max_items_total: 15
engagement_boost: 0.5
clickbait_penalty: 10
outrage_penalty: 10
repetitive_penalty: 10
save_for_later_min_score: 5
digest_days: 1
```

* **Themes** become the digest's sections, in the order written. A `Saved for Later` theme is
  added automatically if you don't define one.
* **weight** scales the LLM's relevance score for that theme (see below).
* **keywords** are hints for the model and the only signal the offline `--llm mock` scorer uses.
* **Avoid** entries are passed to the model as hard negatives.
* Every profile edit is snapshotted in the `interest_profiles` table. Items are scored per
  profile version, so changing the profile re-evaluates items on the next run and every report
  records which profile produced it.

## How scoring works

Each item gets two scores.

**1. `relevance_score` (1-10)** comes straight from the model. The system prompt gives it a rubric
(9-10 "would be annoyed to miss it", 5-6 "tangential or thin", 1-2 "irrelevant or in an avoided
category") together with your profile. In the same structured response the model also returns the
theme, a summary, up to five takeaways and four flags: `is_clickbait`, `is_outrage`,
`is_repetitive`, `save_for_later`.

**2. `final_score` (0-10)** is what actually ranks the digest:

```
final = relevance_score × theme_weight
      + engagement_boost × engagement          # engagement = log10(points + 2×comments + 1) / 4, capped at 1
      − clickbait_penalty  (if flagged)
      − outrage_penalty    (if flagged)
      − repetitive_penalty (if flagged)
clamped to [0, 10]
```

* `theme_weight` lets you say "business news needs to be 20% better to make the cut".
* `engagement_boost` is a tie-breaker, not a popularity contest: at the default 0.5 a viral post
  gains at most half a point.
* Penalties of 10 sink flagged items to the bottom. Flagged items are also **hard-excluded** from
  the digest regardless of score; the penalty only affects how they sort if you query them.
* `min_score` is the bar for the themed sections; `save_for_later_min_score` is the (lower) bar
  for the Saved for Later section; `max_items_per_theme` and `max_items_total` cap the digest.

Repetition is caught twice: cheaply before the LLM call (near-identical titles within the batch
and against the last seven days of digests, keeping the most-engaged copy) and again by the model,
which sees the recently covered titles. Exact duplicates never reach scoring at all because
ingestion dedupes on `(provider, external_id)` **and** on a hash of the canonical URL (tracking
parameters, `www.`, fragments and trailing slashes stripped), so a story shared on HN and Reddit
is stored once.

### LLM settings

| Variable | Default | Notes |
|---|---|---|
| `FEED_FILTER_LLM_PROVIDER` | `anthropic` | `mock` for the offline keyword scorer |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Any current Claude model id |
| `ANTHROPIC_EFFORT` | `medium` | `low`..`max`; controls thinking depth and cost |
| `FEED_FILTER_LLM_BATCH_SIZE` | `12` | Items per request |
| `FEED_FILTER_LLM_MAX_ITEMS` | `200` | Cap per run |

The client asks for JSON-schema-constrained output, caches the system prompt + profile prefix
across batches, opts into Anthropic's server-side refusal fallback (`fallbacks: "default"`) so
a safety decline on one batch is re-run automatically on a recommended model, and retries rate
limits and 5xx errors with backoff. Refusals that survive the fallback, `max_tokens` cut-offs and
invalid JSON raise clear errors instead of silently scoring items.

## Adding API tokens

Copy `.env.example` to `.env`. Everything is optional except the Anthropic key when
`FEED_FILTER_LLM_PROVIDER=anthropic`.

| Source | Needs | How to get it |
|---|---|---|
| **Anthropic** | `ANTHROPIC_API_KEY` | https://platform.claude.com - or run `ant auth login` and leave the variable unset. |
| **Hacker News** | nothing | Uses the public Algolia API. Tune `min_points` / `limit` in `sources.toml`. |
| **RSS** | nothing | List feed URLs under `[rss]` in `sources.toml`. |
| **Reddit** | optional | Anonymous `.json` endpoints work but are rate-limited and sometimes blocked from cloud IPs. For reliability create a *script* app at https://www.reddit.com/prefs/apps and set `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` (the provider fetches a token via the client-credentials flow), or paste a ready `REDDIT_ACCESS_TOKEN`. |
| **X (Twitter)** | one of three | 1. `X_BEARER_TOKEN` from https://developer.x.com (API v2; recent search + user timelines). 2. `X_NITTER_BASE_URL` pointing at a Nitter instance you run; each account becomes `<base>/<handle>/rss`. 3. `X_AUTH_COOKIE` = your browser's `auth_token=...; ct0=...` cookie string, sent to the lightweight syndication timeline endpoint. Strategies are tried in that order. |

Scraping paths (Nitter, syndication) are best-effort plug-in points: X changes them without
notice. If every strategy fails and `FEED_FILTER_ALLOW_MOCK_FALLBACK=true` (the default) the
collector logs the error and substitutes the bundled fixture for that provider so the digest still
runs. Set it to `false` in production once your tokens are in place; failures then surface in the
CLI output and in the report's `stats.providers` block.

Set `FEED_FILTER_PROVIDERS=hackernews,rss` to disable providers you don't use.

### Adding a new provider

1. Create `src/feed_filter/providers/<name>.py` with a class deriving from `Provider`; implement
   `collect()` returning `CollectedItem`s and raise `ProviderError` on recoverable failures.
2. Register a factory in `REGISTRY` in `src/feed_filter/providers/__init__.py`.
3. Optionally drop a `fixtures/<name>.json` so `--mock` and fallback work.
4. Add tests using `httpx.MockTransport` (see `tests/test_providers.py`).

## Dashboard and API

`python main.py --serve` starts FastAPI on http://127.0.0.1:8000:

* `/` - overview with stats and the list of digests
* `/reports/{id}` - rendered digest, `/reports/{id}/raw` - Markdown
* `/api/reports`, `/api/reports/latest`, `/api/reports/{id}`, `/api/stats`, `/health`
* `/api/docs` - OpenAPI UI

The dashboard is read-only and never triggers the pipeline. `--export-html` produces the same
pages as plain files for dropping onto any static host.

## Database

SQLite at `data/feed_filter.db` (override with `FEED_FILTER_DATABASE_URL`; any SQLAlchemy URL
works). Tables:

| Table | Purpose |
|---|---|
| `raw_items` | Every ingested item. Unique on `(provider, external_id)` and on `content_hash`. |
| `filtered_items` | One verdict per `(raw_item, profile)`: relevance, final score, theme, summary, takeaways, flags, model. |
| `interest_profiles` | Snapshots of the profile markdown keyed by hash. |
| `reports` | Digest markdown plus run statistics (collected/duplicates/evaluated/excluded per theme). |

## Tests

```bash
pytest                 # 105 tests, ~5s, fully offline
pytest --cov=feed_filter
ruff check src tests && ruff format --check src tests
```

The suite covers URL normalisation and hashing, every provider against recorded HTTP responses
(`httpx.MockTransport`), duplicate-safe ingestion, profile parsing, scoring maths and repeat
detection, the Claude client's request shape and error paths (against a fake SDK client),
digest selection/rendering, the dashboard and static export, and the CLI end to end.
