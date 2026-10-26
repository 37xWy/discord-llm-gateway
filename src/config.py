import os
from dotenv import load_dotenv

# Load variables from .env into os.environ
load_dotenv()

def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        print(f"❌ CRITICAL CONFIG ERROR: Missing required environment variable '{key}' in environment!", flush=True)
        raise RuntimeError(f"CRITICAL: Missing required environment variable '{key}' in .env")
    return value

DISCORD_BOT_TOKEN = _require_env("DISCORD_BOT_TOKEN")
SUPABASE_URL = _require_env("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _require_env("SUPABASE_SERVICE_ROLE_KEY")
MASTER_ENCRYPTION_KEY = _require_env("MASTER_ENCRYPTION_KEY")
DEMO_GEMINI_API_KEY = _require_env("DEMO_GEMINI_API_KEY")

# Default fallback model for Demo users
DEFAULT_DEMO_MODEL = os.getenv("DEFAULT_DEMO_MODEL", "gemini/gemini-3.8-flash")

# Guardrails & Token Limits
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "800"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "600"))

# Dedicated Channel & System Persona Defaults
DEDICATED_CHAT_CHANNEL_ID = os.getenv("DEDICATED_CHAT_CHANNEL_ID", "").strip()
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "You are a sharp, witty, and concise AI assistant in a shared Discord server. "
    "Pay attention to each user's Discord ID and display name so you remember who is who. "
    "Keep answers direct, engaging, and well-structured.",
)
