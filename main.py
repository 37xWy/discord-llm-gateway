import sys
# Force line-buffering and UTF-8 encoding on stdout/stderr for real-time Render log streaming
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass

print("[STARTUP] main.py loaded. Initializing modules...", flush=True)

import asyncio
import os
import traceback
from contextlib import asynccontextmanager
from typing import Optional
import discord
import uvicorn
from fastapi import FastAPI
from src.config import DISCORD_BOT_TOKEN
from src.db import supabase
from src.bot import bot

bot_startup_error: Optional[str] = None
_shutdown_event = asyncio.Event()


async def _run_bot():
    global bot_startup_error
    backoff = 20  # seconds
    max_backoff = 300  # 5 minutes

    while not _shutdown_event.is_set():
        print("[BOT] Attempting Discord bot connection...", flush=True)
        try:
            bot_startup_error = None
            await bot.start(DISCORD_BOT_TOKEN)
            break
        except asyncio.CancelledError:
            print("[BOT] Bot runner task cancelled for shutdown.", flush=True)
            break
        except discord.HTTPException as e:
            try:
                if hasattr(bot, "http") and bot.http:
                    await bot.http.close()
            except Exception:
                pass

            if e.status == 429:
                retry_after = getattr(e, "retry_after", None)
                wait_time = int(retry_after) if retry_after else backoff
                bot_startup_error = f"Discord 429 Rate Limit (cooling down for {wait_time}s): {e}"
                print(f"[BOT RATE LIMIT] {bot_startup_error}", flush=True)
                print(f"[BOT] Backing off for {wait_time}s before next connection attempt...", flush=True)
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=wait_time)
                    break
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, max_backoff)
            else:
                bot_startup_error = f"Discord HTTPException ({e.status}): {e}"
                print(f"[BOT ERROR] {bot_startup_error}", flush=True)
                traceback.print_exc()
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, max_backoff)
        except Exception as e:
            try:
                if hasattr(bot, "http") and bot.http:
                    await bot.http.close()
            except Exception:
                pass

            bot_startup_error = f"{type(e).__name__}: {e}"
            print(f"[BOT CRASH] {bot_startup_error}", flush=True)
            traceback.print_exc()
            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, max_backoff)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot the Discord bot as a background task when FastAPI starts
    print("[LIFESPAN] Starting up: launching bot background task...", flush=True)
    _shutdown_event.clear()
    bot_task = asyncio.create_task(_run_bot())
    yield
    print("[LIFESPAN] Shutting down: closing bot...", flush=True)
    _shutdown_event.set()
    if not bot.is_closed():
        await bot.close()
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


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
    print(f"[SERVER] Starting Uvicorn on 0.0.0.0:{port}...", flush=True)
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, log_level="info")
