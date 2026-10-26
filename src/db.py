from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
from supabase import create_client, Client
from src.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DEFAULT_DEMO_MODEL
from src.security import encrypt_api_key, hash_passcode

# Initialize Supabase client with service_role key (bypasses RLS safely from backend)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def get_context_key(user_config: Dict[str, Any], discord_user_id: str, discord_channel_id: str) -> str:
    """Determines whether memory is isolated per-user or shared across the channel."""
    scope = user_config.get("memory_scope", "user")
    if scope == "channel":
        return f"channel:{discord_channel_id}"
    return f"user:{discord_user_id}"


def get_user_config(discord_user_id: str) -> Optional[Dict[str, Any]]:
    """Fetches a user's configuration row from Supabase, or None if not registered."""
    res = (
        supabase.table("user_configs")
        .select("*")
        .eq("discord_user_id", str(discord_user_id))
        .maybe_single()
        .execute()
    )
    return res.data if res else None


def redeem_demo_passcode(discord_user_id: str, raw_passcode: str) -> Dict[str, Any]:
    """Verifies a demo passcode and links the Discord user to Demo Mode."""
    code_hash = hash_passcode(raw_passcode)

    # 1. Check if passcode exists, is active, and has remaining uses
    code_res = (
        supabase.table("demo_passcodes")
        .select("*")
        .eq("code_hash", code_hash)
        .maybe_single()
        .execute()
    )
    code_data = code_res.data if code_res else None

    if not code_data or not code_data.get("is_active"):
        return {"success": False, "message": "❌ Invalid or inactive passcode."}

    if code_data["used_count"] >= code_data["max_uses"]:
        return {"success": False, "message": "❌ This passcode has already reached its maximum usage limit."}

    # 2. Upsert user_configs into 'demo' mode
    payload = {
        "discord_user_id": str(discord_user_id),
        "access_mode": "demo",
        "bound_passcode_hash": code_hash,
        "selected_model": DEFAULT_DEMO_MODEL,
    }
    supabase.table("user_configs").upsert(payload).execute()

    remaining = code_data["max_uses"] - code_data["used_count"]
    return {
        "success": True,
        "message": f"✅ Demo unlocked (`{code_data['label']}`)! Remaining shared messages on this code: **{remaining}**.",
    }


def save_user_api_key(discord_user_id: str, raw_api_key: str, model_name: str) -> None:
    """Encrypts the user's LLM API key via Fernet and saves it to Supabase ('byok' mode)."""
    ciphertext = encrypt_api_key(raw_api_key)
    payload = {
        "discord_user_id": str(discord_user_id),
        "access_mode": "byok",
        "encrypted_api_key": ciphertext,
        "key_updated_at": datetime.now(timezone.utc).isoformat(),
        "selected_model": model_name.strip(),
    }
    supabase.table("user_configs").upsert(payload).execute()


def update_user_preferences(
    discord_user_id: str,
    selected_model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    memory_scope: Optional[str] = None,
) -> None:
    """Updates model, system prompt, or memory scope ('user' vs 'channel')."""
    updates: Dict[str, Any] = {}
    if selected_model is not None:
        updates["selected_model"] = selected_model.strip()
    if system_prompt is not None:
        updates["system_prompt"] = system_prompt.strip()
    if memory_scope in ("user", "channel"):
        updates["memory_scope"] = memory_scope

    if updates:
        supabase.table("user_configs").update(updates).eq("discord_user_id", str(discord_user_id)).execute()


def check_rate_limit_and_quota(discord_user_id: str, cooldown_seconds: int = 4) -> Dict[str, Any]:
    """Calls the atomic Postgres RPC to verify cooldowns and deduct demo quota."""
    res = supabase.rpc(
        "check_rate_limit_and_quota",
        {"p_discord_user_id": str(discord_user_id), "p_cooldown_seconds": cooldown_seconds},
    ).execute()
    return res.data


def fetch_conversation_context(context_key: str) -> Dict[str, Any]:
    """Retrieves the non-archived compressed summary + non-archived recent sliding-window messages for a context_key."""
    summary_res = (
        supabase.table("conversation_summaries")
        .select("summary_text")
        .eq("context_key", context_key)
        .eq("is_archived", False)
        .maybe_single()
        .execute()
    )
    summary_text = summary_res.data["summary_text"] if (summary_res and summary_res.data) else ""

    msgs_res = (
        supabase.table("chat_messages")
        .select("id, role, content, created_at")
        .eq("context_key", context_key)
        .eq("is_archived", False)
        .order("created_at", desc=False)
        .execute()
    )
    messages: List[Dict[str, Any]] = msgs_res.data if (msgs_res and msgs_res.data) else []

    return {"summary": summary_text, "messages": messages}


def fetch_referenced_users_context(referenced_users: List[Tuple[str, str]], max_recent: int = 4) -> str:
    """Fetches non-archived read-only summary and recent context for @mentioned users (up to 3)."""
    blocks: List[str] = []
    for uid, display_name in referenced_users[:3]:
        target_key = f"user:{uid}"
        summary_res = (
            supabase.table("conversation_summaries")
            .select("summary_text")
            .eq("context_key", target_key)
            .eq("is_archived", False)
            .maybe_single()
            .execute()
        )
        summary_text = summary_res.data.get("summary_text") if (summary_res and summary_res.data) else ""

        msgs_res = (
            supabase.table("chat_messages")
            .select("role, content, created_at")
            .eq("context_key", target_key)
            .eq("is_archived", False)
            .order("created_at", desc=True)
            .limit(max_recent)
            .execute()
        )
        recent_msgs = msgs_res.data if (msgs_res and msgs_res.data) else []
        recent_msgs.reverse()

        user_lines = [f"### User: {display_name} (Discord ID: {uid})"]
        if summary_text:
            user_lines.append(f"• Summary of past interactions: {summary_text.strip()}")
        if recent_msgs:
            user_lines.append("• Recent messages:")
            for m in recent_msgs:
                user_lines.append(f"  - {m['role'].upper()}: {m['content']}")

        if not summary_text and not recent_msgs:
            user_lines.append("• No prior bot interaction history found.")

        blocks.append("\n".join(user_lines))

    return "\n\n".join(blocks)


def insert_chat_message(
    context_key: str,
    discord_user_id: str,
    discord_channel_id: str,
    role: str,
    content: str,
    model_used: Optional[str] = None,
) -> None:
    """Appends a user or assistant message to the short-term buffer."""
    supabase.table("chat_messages").insert({
        "context_key": context_key,
        "discord_user_id": str(discord_user_id),
        "discord_channel_id": str(discord_channel_id),
        "role": role,
        "content": content,
        "model_used": model_used,
    }).execute()


def insert_chat_turn(
    context_key: str,
    discord_user_id: str,
    discord_channel_id: str,
    user_content: str,
    assistant_content: str,
    model_used: Optional[str] = None,
) -> None:
    """Inserts both user and assistant messages in a single database round-trip."""
    uid = str(discord_user_id)
    cid = str(discord_channel_id)
    supabase.table("chat_messages").insert([
        {
            "context_key": context_key,
            "discord_user_id": uid,
            "discord_channel_id": cid,
            "role": "user",
            "content": user_content,
            "model_used": model_used,
        },
        {
            "context_key": context_key,
            "discord_user_id": uid,
            "discord_channel_id": cid,
            "role": "assistant",
            "content": assistant_content,
            "model_used": model_used,
        },
    ]).execute()


def commit_compression(context_key: str, owner_discord_id: str, new_summary: str, message_ids: List[str]) -> None:
    """Calls the atomic RPC to update the rolling summary and soft-archive the compressed raw messages."""
    supabase.rpc(
        "commit_context_compression",
        {
            "p_context_key": context_key,
            "p_owner_discord_id": str(owner_discord_id),
            "p_new_summary": new_summary,
            "p_message_ids_to_prune": message_ids,
        },
    ).execute()


def clear_memory(context_key: str, wipe_summary: bool = True) -> int:
    """Calls the RPC to soft-archive short-term messages and optionally the compressed summary."""
    res = supabase.rpc(
        "clear_context_memory",
        {"p_context_key": context_key, "p_wipe_summary": wipe_summary},
    ).execute()
    return res.data or 0


def delete_all_user_data(discord_user_id: str) -> None:
    """Self-service privacy wipe: purges encrypted API keys, revokes access, and soft-archives memory."""
    uid = str(discord_user_id)
    # 1. Permanently erase API keys and revoke access in user_configs
    supabase.table("user_configs").update({
        "access_mode": "none",
        "encrypted_api_key": None,
        "bound_passcode_hash": None,
    }).eq("discord_user_id", uid).execute()

    # 2. Soft-archive user's chat messages and summaries
    supabase.table("chat_messages").update({"is_archived": True}).eq("discord_user_id", uid).execute()
    supabase.table("conversation_summaries").update({"is_archived": True}).eq("context_key", f"user:{uid}").execute()
