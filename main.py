import asyncio
import os
import traceback
from contextlib import asynccontextmanager
from typing import Optional
import uvicorn
from fastapi import FastAPI
from src.config import DISCORD_BOT_TOKEN
from src.db import supabase
from src.bot import bot

bot_startup_error: Optional[str] = None


async def _run_bot():
    global bot_startup_error
    print("🚀 Starting Discord bot connection...", flush=True)
    try:
        await bot.start(DISCORD_BOT_TOKEN)
    except Exception as e:
        bot_startup_error = f"{type(e).__name__}: {e}"
        print(f"❌ Discord bot crashed or failed to start: {bot_startup_error}", flush=True)
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot the Discord bot as a background task when FastAPI starts
    bot_task = asyncio.create_task(_run_bot())
    yield
    if not bot.is_closed():
        await bot.close()
    bot_task.cancel()


app = FastAPI(title="Discord LLM Gateway", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """
    Keep-alive and health inspection endpoint for Render.com + Supabase 7-day inactivity heartbeat.
    """
    try:
        await asyncio.to_thread(
            lambda: supabase.table("demo_passcodes").select("label").limit(1).execute()
        )
    except Exception as e:
        print(f"[Heartbeat Warning] Supabase ping failed: {e}", flush=True)

    latency_ms = round(bot.latency * 1000, 2) if bot.is_ready() else None
    return {
        "status": "online",
        "bot_ready": bot.is_ready(),
        "bot_user": str(bot.user) if bot.user else None,
        "discord_latency_ms": latency_ms,
        "startup_error": bot_startup_error,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
