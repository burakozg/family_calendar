"""App-wide configuration. Importing this module loads .env (side effect), so
every module that reads environment variables at import time imports config
first."""
import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL          = "claude-sonnet-4-6"   # model for all AI interactions in the app
