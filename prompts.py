"""
Prompt templates for Indian stock market analysis
"""

import csv
import os
from datetime import datetime

def _load_nifty500_stocks():
    """Load all stocks from Nifty500 list CSV file."""
    stocks_list = []
    csv_path = os.path.join(os.path.dirname(__file__), 'nifty500list.csv')
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_name = row.get('Company Name', '').strip()
                symbol = row.get('Symbol', '').strip()
                if company_name and symbol:
                    stocks_list.append((company_name, symbol))
    except FileNotFoundError:
        print(f"Warning: Nifty500 CSV not found at {csv_path}")
    
    return stocks_list

# Load Nifty500 stocks once at module load time
NIFTY500_STOCKS = _load_nifty500_stocks()

def _format_stocks_for_prompt(stocks_list, max_display=50):
    """Format stocks list for display in prompt."""
    formatted = []
    for idx, (company, symbol) in enumerate(stocks_list, 1):
        formatted.append(f"{idx}. {company} ({symbol}.NS)")
    
    # If more than max_display, show first few and indicate there are more
    if len(formatted) > max_display:
        display_stocks = formatted[:max_display]
        display_stocks.append(f"... and {len(formatted) - max_display} more stocks from Nifty500 list")
        return "\n".join(display_stocks)
    
    return "\n".join(formatted)

def _get_current_quarter():
    """Get current quarter in Indian fiscal year format (FY2024-25 format)."""
    today = datetime.now()
    month = today.month
    year = today.year
    
    # Indian financial year: Apr-Mar
    # Q1: Apr-Jun (months 4-6)
    # Q2: Jul-Sep (months 7-9)
    # Q3: Oct-Dec (months 10-12)
    # Q4: Jan-Mar (months 1-3)
    
    if month >= 4:  # Apr onwards
        fy_start = year
        fy_end = year + 1
        if month >= 10:  # Oct-Dec
            quarter = "Q3"
        elif month >= 7:  # Jul-Sep
            quarter = "Q2"
        else:  # Apr-Jun
            quarter = "Q1"
    else:  # Jan-Mar
        fy_start = year - 1
        fy_end = year
        quarter = "Q4"
    
    return f"{quarter} FY{fy_start}-{str(fy_end)[-2:]}", today.strftime("%Y-%m-%d")

def _get_current_date_formatted():
    """Get current date formatted as 'Month DD, YYYY'."""
    today = datetime.now()
    return today.strftime("%B %d, %Y")

DAILY_MARKET_SUMMARY_PROMPT = """You are an expert Indian stock market analyst specializing in NSE and BSE markets.

Your task is to find and provide TODAY'S QUARTERLY EARNINGS RESULTS AND COMMENTARY from LIVE SOURCES.
Return ONLY valid JSON with NO markdown, NO explanations, NO extra text.

CRITICAL RULES:
1. CHECK MONEYCONTROL AND NSE OFFICIAL SOURCES FIRST for earnings announcements released TODAY ({current_date_formatted}).
2. ONLY include results for stocks from the NIFTY500 list (all 500 stocks).
3. Include {current_quarter} results ONLY if stocks announced results today.
4. DO NOT rely on your training data cutoff - FETCH CURRENT/LIVE DATA from these sources.
5. DO NOT include analyst statements, broker ratings, or news commentaries - ONLY company quarterly results.
6. CRITICAL: Only report results that are ACTUALLY on Moneycontrol/NSE/BSE TODAY. Do NOT assume or fabricate.
7. If none of the Nifty500 stocks announced earnings today, return empty array: {{"today_commentaries": []}}
8. For each confirmed quarterly result (verified from live sources):
   - Extract company name, quarter ({current_quarter}), and result date
   - Include financial metrics: Revenue, Net Profit, EPS with YoY change %
   - Extract exact commentary from company's earnings call or result statement
   - Identify sentiment: BULLISH (beating expectations, strong growth) / BEARISH (missing expectations, weak growth) / NEUTRAL (in-line with expectations)
   - Assess impact: HIGH (major profit beat/miss) / MEDIUM (moderate variance) / LOW (minor variance)
   - Provide short-term forecast (1-4 weeks) based on Q3 results
   - Provide medium-term forecast (1-3 months) based on management guidance
   - Include management guidance if mentioned, else "Not provided"
   - Recommendation: BUY/SELL/HOLD based on results vs expectations
9. All financial figures in INR (Indian Rupees)
10. Return VALID JSON ONLY - no extra text, no markdown

STOCKS TO CHECK (NIFTY500 LIST - ONLY these):
{stocks_list}

DATA SOURCES (Check these FIRST):
1. Moneycontrol (www.moneycontrol.com) - Latest earnings/results section
2. NSE Official Website (www.nseindia.com) - Corporate announcements
3. BSE Official Website (www.bseindia.com) - Announcements
4. Company investor relations pages (official sources)

OUTPUT FORMAT (Return ONLY this JSON structure):
{{
  "today_commentaries": [
    {{
      "stock": "",
      "symbol": "",
      "quarter": "{current_quarter}",
      "result_date": "{current_date_iso}",
      "revenue": "₹XXXXX Cr",
      "revenue_change_yoy": "±X.XX%",
      "profit_net": "₹XXXXX Cr",
      "profit_change_yoy": "±X.XX%",
      "eps": "₹XX.XX",
      "eps_change_yoy": "±X.XX%",
      "commentary_text": "Company's commentary from earnings call or result statement",
      "source": "Moneycontrol/NSE/Company official",
      "sentiment": "BULLISH/BEARISH/NEUTRAL",
      "impact": "HIGH/MEDIUM/LOW",
      "forecast_short_term": "Stock expected to move UP/DOWN/MIXED in next 1-4 weeks based on Q3 results. Reasons: ...",
      "forecast_medium_term": "Stock expected to move UP/DOWN/MIXED in next 1-3 months based on Q3 guidance. Reasons: ...",
      "guidance": "Management guidance for coming quarters if mentioned",
      "recommendation": "BUY/SELL/HOLD"
    }}
  ]
}}"""


def get_daily_market_summary_prompt() -> str:
    """
    Generate a prompt for today's Indian market summary (commentaries only).
    Dynamically loads Nifty500 stocks from CSV file and current date/quarter.
    
    Returns:
        str: Formatted prompt for daily commentaries analysis with all Nifty500 stocks,
             current date, and current quarter
    """
    stocks_formatted = _format_stocks_for_prompt(NIFTY500_STOCKS)
    current_quarter, current_date_iso = _get_current_quarter()
    current_date_formatted = _get_current_date_formatted()
    
    return DAILY_MARKET_SUMMARY_PROMPT.format(
        stocks_list=stocks_formatted,
        current_quarter=current_quarter,
        current_date_iso=current_date_iso,
        current_date_formatted=current_date_formatted
    )