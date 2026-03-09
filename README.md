# Stock Market API

A FastAPI-based REST API that answers stock market queries using Google Generative AI (Gemini).

## Features

- **Stock Market Expertise**: Specialized prompt template for stock market analysis
- **FastAPI**: Modern, fast web framework with automatic API documentation
- **Structured Responses**: Well-defined request/response models using Pydantic
- **Configurable**: Environment-based configuration for easy deployment
- **Error Handling**: Robust error handling and logging
- **Documentation**: Automatic Swagger UI at `/docs`

## Project Structure

```
basilstar/
├── api.py              # Main FastAPI application
├── prompts.py          # Prompt templates for stock market queries
├── config.py           # Configuration management
├── requirements.txt    # Python dependencies
└── README.md          # This file
```

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment


Edit `.env` and add your API key:

```env
GEMINI_API_KEY=your_actual_api_key_here
```

### 3. Run the API

```bash
python api.py
```

The API will start at `http://localhost:8000`

## API Usage

### Health Check

**Endpoint**: `GET /`

```bash
curl http://localhost:8000/
```

**Response**:
```json
{
  "status": "healthy",
  "service": "Stock Market API",
  "model": "gemini-2.0-flash",
  "available_endpoints": [
    "GET /market-summary - Get today's market summary",
    "POST /query - Ask a specific stock market question"
  ]
}
```

### Get Daily Market Summary (NEW!)

**Endpoint**: `GET /market-summary`

This endpoint requires **no parameters** - just hit it and get today's market analysis!

```bash
curl http://localhost:8000/market-summary
```

**Response**:
```json
{
  "date": "2026-02-05",
  "summary": "# Today's Market Summary\n\n## MARKET OVERVIEW\n- S&P 500: ↑ 0.85%...",
  "model_used": "gemini-2.0-flash",
  "success": true
}
```

The summary includes:
- **Market Overview**: Major indices performance (S&P 500, Dow Jones, Nasdaq, etc.)
- **Market Sentiment**: Bullish/bearish sentiment, Fear & Greed Index, key drivers
- **Sector Highlights**: Top and worst performing sectors
- **Top Movers**: Top 5 gainers and losers with reasons
- **Market Commentary**: Expert opinions and outlook
- **Economic Indicators & News**: Important announcements and earnings
- **Trading Insights**: Support/resistance levels, technical analysis

### Query Stock Market

**Endpoint**: `POST /query`

For specific stock market questions:

```bash
curl -X POST "http://localhost:8000/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the current trend of Tesla stock?"}'
```

**Request Body**:
```json
{
  "query": "What is the current trend of Tesla stock?"
}
```

**Response**:
```json
{
  "query": "What is the current trend of Tesla stock?",
  "answer": "Tesla stock has been...",
  "model_used": "gemini-2.0-flash",
  "success": true
}
```

### Interactive API Documentation

Visit `http://localhost:8000/docs` in your browser to access the Swagger UI where you can test endpoints interactively.

## Query Examples

**Daily Market Summary** (no parameters needed):
```bash
curl http://localhost:8000/market-summary
```

**Specific Stock Questions**:
- "What stocks are trending in the tech sector today?"
- "Analyze the performance of Apple Inc. (AAPL) in the last quarter"
- "What are the risks involved in investing in cryptocurrency-related stocks?"
- "Compare Microsoft and Google stock performance"
- "What sectors are expected to perform well in the coming months?"

## Prompt Template

The API uses a specialized prompt template (`prompts.py`) that instructs the Gemini LLM to:

1. **Reference accurate sources**: Yahoo Finance, Bloomberg, Reuters, CNBC, etc.
2. **Structure responses** with:
   - Executive summary
   - Key facts and developments
   - Relevant statistics
   - Risk assessment
   - Actionable insights

3. **Format requirements**:
   - Professional language
   - Stock symbols (AAPL, MSFT, etc.)
   - Dates for recent events
   - Bullet points for key info
   - Price ranges and % changes

4. **Include disclaimers** about financial advice

## Configuration

Edit `config.py` or `.env` to customize:

- `GEMINI_API_KEY`: Your Google Generative AI API key
- `GEMINI_MODEL`: Model to use (default: "gemini-2.0-flash")
- `API_PORT`: Port to run the API on (default: 8000)
- `API_HOST`: Host to bind to (default: 0.0.0.0)
- `DEBUG`: Enable debug mode (default: False)
- `MAX_QUERY_LENGTH`: Maximum query length in characters (default: 1000)

## Requirements

- Python 3.8+
- google-genai
- fastapi
- uvicorn
- pydantic
- python-dotenv

## Error Handling

The API returns appropriate HTTP status codes:

- `200`: Successful query
- `422`: Validation error (invalid request)
- `500`: Server error (GenAI API error, etc.)

## Notes

- This is a demo API. For production use, consider adding:
  - Authentication/Authorization
  - Rate limiting
  - Request/Response caching
  - Logging to external services
  - Database for query history
  - Input sanitization
- The GenAI responses are based on training data and may not reflect real-time market data
- Always verify financial information with official sources
- Consider consulting financial advisors for investment decisions

## License

MIT
