"""
Configuration settings for the Stock Market API
"""

import os
from dotenv import load_dotenv

load_dotenv()

# API Configuration
API_PORT = int(os.getenv("API_PORT", 8000))
API_HOST = os.getenv("API_HOST", "0.0.0.0")
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# GenAI Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# API Settings
MAX_QUERY_LENGTH = 1000
TIMEOUT_SECONDS = 30
