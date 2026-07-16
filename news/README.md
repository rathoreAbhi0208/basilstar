# Basilstar News Engine v3.0.0

Real-time AI-enriched Indian Financial News system.

## Architecture

```
news/
├── __init__.py          # Package entry-point
├── config.py            # Settings, thresholds, market schedule logic
├── models.py            # Pydantic data models
├── fetcher.py           # Async RSS fetcher (Google News + Official Sources)
├── generator.py         # Two-Stage Gemini AI enrichment pipeline
├── image_resolver.py    # Multi-provider image resolver (Wikimedia, Pexels, Unsplash)
├── prompts.py           # Gemini prompt templates + output schemas
├── db.py                # Async SQLite persistence (WAL, 24h TTL)
├── scheduler.py         # Background unified fetch scheduler (15/30/60 min)
├── api.py               # FastAPI sub-application endpoints
└── earnings/            # Dedicated earnings pipeline module
    ├── __init__.py      # Package entry-point
    ├── generator.py     # Earnings data collection and gap-fill logic
    ├── models.py        # Earnings-specific Pydantic data models
    └── prompts.py       # Earnings-specific prompt templates
```

## Pipeline (per cycle)

```
RSS Fetcher (Google News + NSE/SEBI) 
         │ (URL Dedup)
         ▼
Gemini Stage 1: Market Intelligence Evaluation
         │ (Scoring: Intraday, Swing, Structural, Classification)
         ├──► (Regular News) ──► Threshold Filter (High >= 80, Medium >= 50)
         │                           │ (Discard low-relevance items)
         │                           ▼
         │                       Gemini Stage 2: Premium Article Generation
         │                           │ (500-word stories, Sentiment, Time Horizon)
         │
         └──► (Earnings Result) ─► Earnings Pipeline: Financial Data Collection
                                     │ (Targeted search + URL Context + Gap-fill)
                                     ▼
                                 Earnings Pipeline: Financial Analysis
                                     │ (Detailed evaluation & scoring)
         ▼
Image Resolver (Cache → Wikimedia → Pexels → Unsplash)
         │
         ▼
SQLite DB (news_articles & raw_news_items)
```

## Fetch Schedule

| Market State         | Interval |
|----------------------|----------|
| Open (09:00–15:30)   | 15 min    |
| After-market (–20:00)| 30 min   |
| Night / Weekend      | 60 min   |

## API Endpoints

| Method | Path            | Description                           |
|--------|-----------------|---------------------------------------|
| GET    | `/news/`        | Paginated AI-enriched articles        |
| GET    | `/news/raw`     | Raw fetched items + Stage 1 scores    |
| GET    | `/news/status`  | Scheduler health + DB stats           |
| POST   | `/news/refresh` | Manual trigger (ops/dev)              |

### Query Parameters for `GET /news/` and `GET /news/raw`

| Param                | Example                | Description                          |
|----------------------|------------------------|--------------------------------------|
| `page`               | `1`                    | Page number                          |
| `page_size`          | `20`                   | Items per page (max 100)             |
| `sentiment`          | `Positive`             | Filter by sentiment                  |
| `market_impact_level`| `High`                 | Filter by market impact level        |
| `time_horizon`       | `short_term_catalyst`  | `short_term_catalyst`\|`long_term_structural`\|`both` |
| `source`             | `NSE`                  | Filter by source (e.g. NSE, SEBI)    |
| `company`            | `HDFC`                 | Filter by company                    |
| `sector`             | `Banking`              | Filter by sector                     |
| `tag`                | `RBI`                  | Filter by tag                        |
| `search`             | `repo rate`            | Full-text search (only `/news/`)     |

## Environment Variables

```env
# Required
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash

# Image Resolution Providers
IMAGE_PROVIDER_PRIORITY=wikimedia,pexels,unsplash
PEXELS_API_KEY=your_pexels_key
UNSPLASH_ACCESS_KEY=your_unsplash_key

# Stage 1 Thresholds
STAGE1_HIGH_THRESHOLD=80
STAGE1_MEDIUM_THRESHOLD=50
STAGE1_GENERATE_MEDIUM=true

# Batching (Token Management)
STAGE1_BATCH_SIZE=15
STAGE2_BATCH_SIZE=8

# Earnings Pipeline
EARNINGS_MIN_COMPLETENESS_PCT=60

# Tuning
NEWS_DB_PATH=news.db
NEWS_RETENTION_HOURS=24
NEWS_INTERVAL_OPEN=900       # 15 min (market open)
NEWS_INTERVAL_CLOSED=1800    # 30 min (after market)
NEWS_INTERVAL_NIGHT=3600     # 60 min (night/weekend)
NEWS_MAX_RETRIES=3
NEWS_TEMPERATURE=0.0
```

## Setup

```bash
pip install -r requirements.txt

# Run
uvicorn news.api:news_app --host 0.0.0.0 --port 8000 --reload
```

## Key Design Decisions

- **Two-Stage Pipeline**: Decouples intelligence evaluation from premium article generation. Stage 1 acts as a data-driven filter scoring items against multiple trading personas.
- **Threshold-Driven Generation**: Decision logic uses `STAGE1_HIGH_THRESHOLD` and `STAGE1_MEDIUM_THRESHOLD` on `market_relevance_score` to determine article generation (replacing legacy LLM booleans).
- **Dedicated Earnings Pipeline**: Items classified as earnings results bypass the normal filter and trigger a specialized financial data collection and analysis pipeline.
- **Targeted Gap-Fill Mechanism**: The earnings data collection self-assesses its `data_completeness_pct` and automatically executes a targeted secondary fetch for any missing financial fields before proceeding to analysis.
- **Robust JSON Healing**: Uses `json-repair` to dynamically recover broken Gemini JSON outputs (unescaped chars, missing colons) avoiding pipeline crashes.
- **Deduplication**: URL-hash based UIDs persist in `raw_news_items`, bypassing erratic or static timestamps published by official exchange RSS feeds. 
- **Image Fallback Chain**: High-performance image discovery prioritizing keyless open-source providers (Wikimedia) before falling back to quota-limited premium APIs (Pexels, Unsplash).
- **Date parsing**: Uses `python-dateutil` to handle dozens of non-standard date formats emitted by varied Indian regulatory feeds.
