from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import logging
import json
from datetime import datetime
from config import GEMINI_API_KEY, GEMINI_MODEL
from prompts import get_daily_market_summary_prompt

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Stock Market API",
    description="API to get daily market summary using Google Generative AI",
    version="1.0.0",
    docs_url="/api/docs"
)

# Initialize GenAI client
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found in environment variables")

client = genai.Client(api_key=GEMINI_API_KEY)

config = types.GenerateContentConfig(
    temperature=0.0,
    top_p=0.1,
    tools=[types.Tool(
        google_search=types.GoogleSearch()
    )]
)


# Request/Response Models
class DailyMarketSummaryResponse(BaseModel):
    date: str = Field(description="Date of the market summary")
    market_data: dict = Field(description="Structured market data with indices, sectors, movers")
    model_used: str = Field(description="GenAI model used for response")
    success: bool = Field(description="Whether the request was successful")


# Routes
@app.get("/", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",  
        "service": "Stock Market API",
        "model": GEMINI_MODEL
    }


@app.get("/market-summary", response_model=DailyMarketSummaryResponse, tags=["Market"])
async def get_daily_market_summary():
    """
    Get today's market summary including sentiment, highlights, and commentary.
    No parameters needed - automatically analyzes current market conditions.
    
    Returns:
        DailyMarketSummaryResponse with today's market analysis
        
    Raises:
        HTTPException: If there's an error during processing
    """
    try:
        logger.info("Processing daily market summary request")
        
        # Get the prompt for daily market summary
        prompt = get_daily_market_summary_prompt()
        
        # Call GenAI API
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config
        )

        # Extract summary text
        summary_text = response.text if hasattr(response, 'text') else str(response)
        
        # Parse JSON response
        # try:
        #     market_data = json.loads(summary_text)
        # except json.JSONDecodeError:
        #     logger.warning("Failed to parse JSON, returning raw response")
        #     market_data = {"raw_response": summary_text}
        def extract_json(text: str):
            text = text.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                import re
                json_match = re.search(r'\{.*\}', text, re.S)
                if not json_match:
                    raise ValueError("No JSON object found in the response")
                return json.loads(json_match.group())
        
        market_data = extract_json(summary_text)

        
        logger.info("Daily market summary generated successfully")
        
        return DailyMarketSummaryResponse(
            date=datetime.now().strftime("%Y-%m-%d"),
            market_data=market_data,
            model_used=GEMINI_MODEL,
            success=True
        )
        
    except Exception as e:
        logger.error(f"Error generating market summary: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error generating market summary: {str(e)}"
        )


# @app.get("/docs", include_in_schema=False)
# async def get_docs():
#     """Swagger UI documentation"""
#     pass


if __name__ == "__main__":
    import uvicorn
    from config import API_PORT, API_HOST, DEBUG
    
    uvicorn.run(
        app,
        host=API_HOST,
        port=API_PORT,
        reload=DEBUG,
        log_level="info"
    )
