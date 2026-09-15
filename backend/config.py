"""App-wide configuration. Importing this module loads .env (side effect), so
every module that reads environment variables at import time imports config
first."""
from dotenv import load_dotenv

load_dotenv()

AI_MODEL = "qwen/qwen3-vl-235b-a22b-instruct"   # model for all AI interactions in the app
