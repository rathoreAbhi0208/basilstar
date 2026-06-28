# Basilstar News Package v2.0

Real-time AI-enriched Indian Financial News system.

## Architecture

```
news/
├── __init__.py          # Package entry-point
├── config.py            # All settings, market schedule logic
├── models.py            # Pydantic data models
├── fetcher.py           # Async RSS fetcher (18 Tier-1 + Tier-2 sources)
├── generator.py         # Gemini AI enrichment pipeline
├── image_resolver.py    # Image URL resolver (Google CSE → Wikimedia → fallback)
├── prompts.py           # Gemini prompt templates + output schemas
├── db.py                # Async SQLite persistence (WAL, 24h TTL)
├── scheduler.py         # Background fetch scheduler (5/15/30 min)
└── news_api.py          # FastAPI sub-application
```

## Pipeline (per cycle)

```
RSS Fetcher (18 sources) → Gemini Enrichment → Image Resolver → SQLite DB
     │                           │                    │
     │  Tier-1: NSE/BSE/SEBI     │  500-word stories  │  Google CSE
     │  Tier-2: ET/MC/Mint...    │  Sentiment/Impact  │  Wikimedia
     │                           │  Tags/Keywords     │  Static fallbacks
     └─── deduplicated by URL ───┘                    └── HEAD-verified URLs
```

## Fetch Schedule

| Market State         | Interval |
|----------------------|----------|
| Open (09:00–15:30)   | 5 min    |
| After-market (–20:00)| 15 min   |
| Night / Weekend      | 30 min   |

## API Endpoints

| Method | Path            | Description                           |
|--------|-----------------|---------------------------------------|
| GET    | `/news/`        | Paginated news list (rich filtering)  |
| GET    | `/news/status`  | Scheduler health + DB stats           |
| GET    | `/news/{id}`    | Single article                        |
| POST   | `/news/refresh` | Manual trigger (ops/dev)              |

### Query Parameters for `GET /news/`

| Param      | Example          | Description                          |
|------------|------------------|--------------------------------------|
| `page`     | `1`              | Page number                          |
| `page_size`| `20`             | Items per page (max 100)             |
| `category` | `IPO`            | Filter by category                   |
| `sentiment`| `Positive`       | Filter by sentiment                  |
| `impact`   | `High`           | Filter by impact level               |
| `tier`     | `1`              | Source tier (1=official, 2=media)    |
| `company`  | `HDFC`           | Filter by company                    |
| `sector`   | `Banking`        | Filter by sector                     |
| `tag`      | `RBI`            | Filter by tag                        |
| `search`   | `repo rate`      | Full-text search                     |
| `sort`     | `importance`     | newest \| oldest \| importance       |

## Environment Variables

```env
# Required
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash

# Optional — Image resolution (Tier-1)
GOOGLE_API_KEY=your_google_api_key
GOOGLE_CX=your_custom_search_engine_id

# Optional — Tuning
NEWS_DB_PATH=news.db
NEWS_RETENTION_HOURS=24
NEWS_INTERVAL_OPEN=300       # 5 min (market open)
NEWS_INTERVAL_CLOSED=900     # 15 min (after market)
NEWS_INTERVAL_NIGHT=1800     # 30 min (night/weekend)
NEWS_ARTICLES_PER_CYCLE=15
NEWS_MAX_RETRIES=3
NEWS_TEMPERATURE=0.1
```

## Setup

```bash
pip install -r requirements.txt

# Optionally set up Google Custom Search for better images:
# 1. Create a project at https://console.cloud.google.com
# 2. Enable "Custom Search API"
# 3. Create a Custom Search Engine at https://cse.google.com
# 4. Set GOOGLE_API_KEY and GOOGLE_CX env vars

# Run
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

## Mounting in parent app

```python
# api.py
from news import news_app
app.mount("/news", news_app)
```

## Key Design Decisions

- **Deduplication**: Articles are identified by `sha256(headline)`. The DB uses `INSERT OR IGNORE` to prevent duplicates.
- **Image verification**: All image URLs are HEAD-verified before storage. Falls back through 3 layers.
- **24h TTL**: Expired articles are auto-pruned each scheduler cycle.
- **Circuit breaker**: Exponential back-off with `max_retries` before giving up a cycle.
- **WAL mode**: SQLite in Write-Ahead Logging mode for concurrent reads/writes.
