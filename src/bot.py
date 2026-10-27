import asyncio
import traceback
from typing import Literal, Optional, List, Tuple
import discord
from discord import app_commands
from discord.ext import commands
from src.config import MAX_PROMPT_CHARS, DEDICATED_CHAT_CHANNEL_ID
from src.db import (
    get_user_config,
    get_context_key,
    redeem_demo_passcode,
    save_user_api_key,
    update_user_preferences,
    check_rate_limit_and_quota,
    fetch_conversation_context,
    clear_memory,
    delete_all_user_data,
)
from src.llm import generate_chat_reply


# Reusable allowed mentions: Allow user mentions and reply highlights while blocking @everyone and @roles
SAFE_MENTIONS = discord.AllowedMentions(
    everyone=False,
    roles=False,
    users=True,
    replied_user=True,
)


# ============================================================================
# 1. SECURE POP-UP MODALS (For Passcodes & BYOK API Keys)
# ============================================================================

class DemoUnlockModal(discord.ui.Modal, title="Unlock Bot (Demo Passcode)"):
    passcode_input = discord.ui.TextInput(
        label="Enter Demo / Employer Passcode",
        placeholder="e.g., PORTFOLIO-DEMO",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(
            redeem_demo_passcode, str(interaction.user.id), self.passcode_input.value
        )
        await interaction.followup.send(result["message"], ephemeral=True)


class BYOKSetupModal(discord.ui.Modal, title="Configure Your Own LLM API Key"):
    model_input = discord.ui.TextInput(
        label="LiteLLM Model Identifier",
        placeholder="e.g., gemini/gemini-2.5-flash, openai/gpt-4o-mini, anthropic/claude-3-5-sonnet",
        default="gemini/gemini-3.8-flash",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    api_key_input = discord.ui.TextInput(
        label="API Key (Encrypted via Fernet before saving)",
        placeholder="Paste your provider API key here...",
        style=discord.TextStyle.short,
        required=True,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(
            save_user_api_key,
            str(interaction.user.id),
            self.api_key_input.value,
            self.model_input.value,
        )
        await interaction.followup.send(
            f"🔐 **API Key Encrypted & Saved!**\n"
            f"• **Mode:** `byok` (Unlimited usage with your key)\n"
            f"• **Active Model:** `{self.model_input.value.strip()}`\n"
            f"You can now `@mention` me to chat!",
            ephemeral=True,
        )


# ============================================================================
# 2. BOT INITIALIZATION & SLASH COMMANDS
# ============================================================================

class GatewayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # Required to read chat messages
        super().__init__(command_prefix="!", intents=intents, allowed_mentions=SAFE_MENTIONS)

    async def setup_hook(self):
        # Sync global slash commands on startup
        await self.tree.sync()
        print("Slash commands synced globally.", flush=True)

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})", flush=True)


bot = GatewayBot()


@bot.tree.command(name="unlock", description="Unlock the bot using a demo/recruiter passcode")
async def unlock_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(DemoUnlockModal())


@bot.tree.command(name="setup_key", description="Securely configure your own LLM API key (Fernet encrypted)")
async def setup_key_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(BYOKSetupModal())


@bot.tree.command(name="config", description="View or update your model or system prompt")
@app_commands.describe(
    model="LiteLLM model string (only applies in BYOK mode)",
    system_prompt="Custom persona instructions for the AI",
)
async def config_cmd(
    interaction: discord.Interaction,
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    cfg = await asyncio.to_thread(get_user_config, user_id)

    if not cfg or cfg.get("access_mode") == "none":
        await interaction.followup.send(
            "⚠️ You haven't unlocked the bot yet! Run `/unlock` (passcode) or `/setup_key` (BYOK) first.",
            ephemeral=True,
        )
        return

    if any(x is not None for x in (model, system_prompt)):
        await asyncio.to_thread(
            update_user_preferences, user_id, model, system_prompt, None
        )
        cfg = await asyncio.to_thread(get_user_config, user_id)

    await interaction.followup.send(
        f"⚙️ **Your Current Configuration**\n"
        f"• **Access Mode:** `{cfg['access_mode']}`\n"
        f"• **Selected Model:** `{cfg['selected_model']}`\n"
        f"• **Memory Scope:** `{cfg['memory_scope']}` (locked to personal user isolation)\n"
        f"• **Messages Sent:** `{cfg['total_messages_sent']}`\n"
        f"• **System Prompt:** `{cfg['system_prompt'][:150]}`",
        ephemeral=True,
    )


@bot.tree.command(name="memory", description="Inspect or clear your conversation memory and compressed summary")
@app_commands.describe(action="View current memory status, clear short-term only, or wipe everything")
async def memory_cmd(
    interaction: discord.Interaction,
    action: Literal["status", "clear_recent", "wipe_all"] = "status",
):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    channel_id = str(interaction.channel_id)
    cfg = await asyncio.to_thread(get_user_config, user_id)

    if not cfg:
        await interaction.followup.send("⚠️ No configuration found. Use `/unlock` or `/setup_key` first.", ephemeral=True)
        return

    context_key = get_context_key(cfg, user_id, channel_id)

    if action == "status":
        mem = await asyncio.to_thread(fetch_conversation_context, context_key)
        summary_preview = mem["summary"] or "*No compressed summary yet.*"
        await interaction.followup.send(
            f"🧠 **Memory State (`{context_key}`)**\n"
            f"• **Active Short-Term Messages:** `{len(mem['messages'])} / {cfg['max_context_messages']}`\n"
            f"• **Compressed Rolling Summary:**\n> {summary_preview[:500]}",
            ephemeral=True,
        )
    elif action == "clear_recent":
        deleted = await asyncio.to_thread(clear_memory, context_key, False)
        await interaction.followup.send(
            f"🧹 Cleared **{deleted}** short-term messages (kept your long-term compressed summary).",
            ephemeral=True,
        )
    elif action == "wipe_all":
        deleted = await asyncio.to_thread(clear_memory, context_key, True)
        await interaction.followup.send(
            f"🗑️ Wiped **{deleted}** short-term messages AND cleared the compressed summary for `{context_key}`.",
            ephemeral=True,
        )


@bot.tree.command(name="delete_my_data", description="Privacy wipe: Permanently purge your API key, reset access, and clear active memory")
async def delete_my_data_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await asyncio.to_thread(delete_all_user_data, str(interaction.user.id))
    await interaction.followup.send(
        "🛡️ **Privacy Wipe Complete:** Your encrypted API key has been permanently purged, your access has been reset, and your active bot memory has been cleared.",
        ephemeral=True,
    )


# ============================================================================
# 3. NATURAL CONVERSATION LISTENER (Server Channels: @Mentions, Replies, or Dedicated Channel)
# ============================================================================

def _chunk_text(text: str, limit: int = 1950):
    """Splits long LLM replies so they never exceed Discord's 2000-character message limit."""
    chunks = [text[i : i + limit] for i in range(0, len(text), limit)] or ["(Empty response)"]
    print(f"[REPLY] Chunked message into {len(chunks)} part(s) (total chars: {len(text)})", flush=True)
    return chunks


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 1. Disable Direct Messages (DMs) - only operate inside guilds
    if message.guild is None:
        return

    # 2. Ignore messages prefixed with //
    if message.content.strip().startswith("//"):
        return

    # 3. Dedicated Channel Trigger, Mentions, or Direct Replies
    is_dedicated_channel = bool(DEDICATED_CHAT_CHANNEL_ID and str(message.channel.id) == DEDICATED_CHAT_CHANNEL_ID)
    is_mentioned = bot.user in message.mentions if bot.user else False
    is_reply_to_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and getattr(message.reference.resolved, "author", None) == bot.user
    )

    if not (is_mentioned or is_reply_to_bot or is_dedicated_channel):
        return

    # 4. Block Attachments
    if message.attachments:
        await message.reply(
            "📎 File and media attachments are disabled to conserve tokens. Please send plain text only.",
            allowed_mentions=SAFE_MENTIONS,
        )
        return

    # 5. Clean Prompt & Extract Referenced @Users
    clean_prompt = message.content
    if bot.user:
        clean_prompt = clean_prompt.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    referenced_users: List[Tuple[str, str]] = []
    seen_uids = set()
    for u in message.mentions:
        if bot.user and u.id == bot.user.id:
            continue
        clean_prompt = clean_prompt.replace(f"<@{u.id}>", f"@{u.display_name} (ID: {u.id})").replace(f"<@!{u.id}>", f"@{u.display_name} (ID: {u.id})")
        uid_str = str(u.id)
        if not u.bot and uid_str not in seen_uids:
            seen_uids.add(uid_str)
            if len(referenced_users) < 3:
                referenced_users.append((uid_str, u.display_name))

    clean_prompt = clean_prompt.strip()

    if not clean_prompt:
        await message.reply(
            "👋 Hi! Use `/unlock` (demo passcode) or `/setup_key` (bring your own API key) to configure me, then ask me anything!",
            allowed_mentions=SAFE_MENTIONS,
        )
        return

    # 6. Enforce Input Character Limit (MAX_PROMPT_CHARS) before touching DB/quota
    if len(clean_prompt) > MAX_PROMPT_CHARS:
        await message.reply(
            f"⚠️ Prompt too long ({len(clean_prompt)}/{MAX_PROMPT_CHARS} chars). Please keep messages concise.",
            allowed_mentions=SAFE_MENTIONS,
        )
        return

    # 7. Extract Single-Message Reply Context (Even when replying to another human)
    reply_context: Optional[str] = None
    if (
        message.reference is not None
        and message.reference.resolved is not None
        and isinstance(message.reference.resolved, discord.Message)
    ):
        ref_msg = message.reference.resolved
        reply_context = f"[Author: {ref_msg.author.display_name} (Discord ID: {ref_msg.author.id})]: {ref_msg.content[:400]}"

    user_id = str(message.author.id)
    channel_id = str(message.channel.id)

    # 8. Check Rate Limit + Demo Quota via Atomic Supabase RPC
    import time
    t_gate_start = time.perf_counter()
    print(f"\n[MSG RECEIVED] From {message.author} -> Checking Supabase quota...", flush=True)

    gate = await asyncio.to_thread(check_rate_limit_and_quota, user_id, 4)
    print(f"[TIMING] 0. Supabase quota RPC: {time.perf_counter() - t_gate_start:.2f}s", flush=True)

    if not gate.get("allowed"):
        reason_str = str(gate.get("reason", ""))
        # Silently ignore unverified users chatting in the dedicated channel without directly tagging/replying to the bot
        if is_dedicated_channel and not (is_mentioned or is_reply_to_bot) and "Unauthorized" in reason_str:
            return
        await message.reply(
            f"🔒 {reason_str}",
            allowed_mentions=SAFE_MENTIONS,
        )
        return

    # 9. Fetch User Config & Generate LLM Response with Deep Diagnostics
    async with message.channel.typing():
        try:
            print(f"[LLM] Fetching user config for {user_id}...", flush=True)
            user_cfg = await asyncio.to_thread(get_user_config, user_id)
            print(f"[LLM] Generating reply for {message.author} (prompt length: {len(clean_prompt)} chars)...", flush=True)

            reply_text = await generate_chat_reply(
                user_config=user_cfg,
                discord_user_id=user_id,
                discord_display_name=message.author.display_name,
                discord_channel_id=channel_id,
                user_message=clean_prompt,
                reply_context=reply_context,
                referenced_users=referenced_users,
            )
            print(f"[LLM] Reply generated successfully ({len(reply_text)} chars). Sending to Discord...", flush=True)

            # Append subtle quota footer if user is on Demo Passcode mode
            remaining = gate.get("remaining_demo_uses", -1)
            if remaining >= 0 and remaining <= 10:
                reply_text += f"\n\n*(Demo quota remaining: {remaining} messages)*"

            for chunk in _chunk_text(reply_text):
                await message.reply(
                    chunk,
                    allowed_mentions=SAFE_MENTIONS,
                )
            print(f"[REPLY] Finished sending reply chunks to {message.author}", flush=True)

        except Exception as e:
            print(f"[CRITICAL ERROR] Failed to process message for {message.author}: {e}", flush=True)
            traceback.print_exc()
            try:
                await message.reply(
                    "⚠️ An internal error occurred while generating your response. Please try again later.",
                    allowed_mentions=SAFE_MENTIONS,
                )
            except Exception:
                pass
