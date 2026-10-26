import asyncio
import os
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI
from src.config import DISCORD_BOT_TOKEN
from src.db import supabase
from src.bot import bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot the Discord bot as a background task when FastAPI starts
    bot_task = asyncio.create_task(bot.start(DISCORD_BOT_TOKEN))
    yield
    if not bot.is_closed():
        await bot.close()
    bot_task.cancel()


app = FastAPI(title="Discord LLM Gateway", lifespan=lifespan)


@app.get("/health")
async def health_check():
    """
    Keep-alive endpoint for Render.com + Supabase 7-day inactivity heartbeat.
    """
    await asyncio.to_thread(
        lambda: supabase.table("demo_passcodes").select("label").limit(1).execute()
    )
    latency_ms = round(bot.latency * 1000, 2) if bot.is_ready() else -1
    return {
        "status": "online",
        "bot_ready": bot.is_ready(),
        "discord_latency_ms": latency_ms,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)