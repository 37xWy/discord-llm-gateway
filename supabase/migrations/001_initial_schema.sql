-- ============================================================================
-- 1. EXTENSIONS
-- ============================================================================
create extension if not exists pgcrypto;
create extension if not exists vector;

-- ============================================================================
-- 2. TABLES
-- ============================================================================

-- Table A: Employer / Friend Demo Passcodes (Protects your free Gemini API key)
create table public.demo_passcodes (
    code_hash text primary key,              -- SHA-256 hash of passcode
    label text not null,                     -- e.g., 'Employer_Demo' or 'Friends_Server'
    max_uses integer not null default 30,    -- Hard cap so no one drains your free tier
    used_count integer not null default 0,
    is_active boolean not null default true,
    expires_at timestamptz,
    created_at timestamptz not null default now()
);

-- Table B: User Credentials, Model Config & Anti-Spam Tracking
create table public.user_configs (
    discord_user_id text primary key,
    access_mode text not null default 'none' check (access_mode in ('none', 'demo', 'byok')),
    
    -- BYOK (Bring Your Own Key) — Encrypted in Python via Fernet before saving
    encrypted_api_key text,
    key_updated_at timestamptz,
    
    -- Demo Mode Link (Uses your free-tier Gemini key under strict quota)
    bound_passcode_hash text references public.demo_passcodes(code_hash) on delete set null,
    
    -- Model & Persona Configuration
    selected_model text not null default 'gemini/gemini-3.8-flash',
    system_prompt text not null default 'You are a helpful, concise AI assistant inside Discord.',
    temperature numeric(3,2) not null default 0.70 check (temperature >= 0.0 and temperature <= 2.0),
    
    -- Memory Preferences
    memory_scope text not null default 'user' check (memory_scope in ('user', 'channel')),
    max_context_messages integer not null default 12 check (max_context_messages between 4 and 50),
    
    -- Anti-Spam & Telemetry
    last_message_at timestamptz,             -- Used to enforce cooldowns against spam
    total_messages_sent integer not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Table C: Compressed Long-Term Context Summaries
-- Keyed by context_key (either a discord_user_id for personal memory, or discord_channel_id for shared server memory)
create table public.conversation_summaries (
    context_key text primary key,            -- e.g., 'user:123456' or 'channel:987654'
    owner_discord_id text references public.user_configs(discord_user_id) on delete cascade,
    summary_text text not null default '',
    is_archived boolean not null default false,
    updated_at timestamptz not null default now()
);

-- Table D: Short-Term Sliding Window Chat Messages
create table public.chat_messages (
    id uuid primary key default gen_random_uuid(),
    context_key text not null,               -- Matches 'user:<id>' or 'channel:<id>'
    discord_user_id text not null references public.user_configs(discord_user_id) on delete cascade,
    discord_channel_id text not null,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    model_used text,
    embedding vector(768),
    is_archived boolean not null default false,
    created_at timestamptz not null default now()
);

-- ============================================================================
-- 3. INDEXES & RLS LOCKDOWN
-- ============================================================================
create index idx_chat_messages_context_time 
    on public.chat_messages (context_key, created_at asc);

alter table public.demo_passcodes enable row level security;
alter table public.user_configs enable row level security;
alter table public.conversation_summaries enable row level security;
alter table public.chat_messages enable row level security;

-- Ensure service_role has full access even if "Automatically expose new tables" was unchecked
grant all on table public.demo_passcodes to service_role;
grant all on table public.user_configs to service_role;
grant all on table public.conversation_summaries to service_role;
grant all on table public.chat_messages to service_role;

-- ============================================================================
-- 4. ATOMIC RPC FUNCTIONS (Anti-Spam, Quota & Memory Management)
-- ============================================================================

-- Trigger: Auto-update updated_at timestamp on user_configs
create or replace function public.handle_updated_at()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger set_user_configs_updated_at
    before update on public.user_configs
    for each row execute function public.handle_updated_at();

-- RPC 1: Anti-Spam + Demo Quota Gatekeeper
-- Checks cooldown (e.g., 4 seconds between messages) AND deducts 1 demo credit if in 'demo' mode
create or replace function public.check_rate_limit_and_quota(
    p_discord_user_id text,
    p_cooldown_seconds integer default 4
)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
    v_user record;
    v_remaining integer;
begin
    select * into v_user
    from public.user_configs
    where discord_user_id = p_discord_user_id;

    if not found or v_user.access_mode = 'none' then
        return jsonb_build_object('allowed', false, 'reason', 'Unauthorized. Use /unlock or /setup_key first.');
    end if;

    -- 1. Anti-Spam Cooldown Check
    if v_user.last_message_at is not null and 
       v_user.last_message_at > (now() - make_interval(secs => p_cooldown_seconds)) then
        return jsonb_build_object('allowed', false, 'reason', 'Cooldown active. Please wait a few seconds between messages.');
    end if;

    -- 2. If using YOUR Gemini key ('demo' mode), enforce passcode max_uses
    if v_user.access_mode = 'demo' then
        update public.demo_passcodes
        set used_count = used_count + 1
        where code_hash = v_user.bound_passcode_hash
          and is_active = true
          and used_count < max_uses
          and (expires_at is null or expires_at > now())
        returning (max_uses - used_count) into v_remaining;

        if not found then
            return jsonb_build_object('allowed', false, 'reason', 'Demo passcode quota exhausted! Add your own API key via /setup_key to continue.');
        end if;
    else
        v_remaining := -1; -- Unlimited for BYOK users using their own API key
    end if;

    -- 3. Update last_message_at and counter
    update public.user_configs
    set last_message_at = now(),
        total_messages_sent = total_messages_sent + 1
    where discord_user_id = p_discord_user_id;

    return jsonb_build_object(
        'allowed', true,
        'access_mode', v_user.access_mode,
        'remaining_demo_uses', v_remaining
    );
end;
$$;

-- RPC 2: Commit Context Compression (Saves new summary + soft-archives old raw messages)
create or replace function public.commit_context_compression(
    p_context_key text,
    p_owner_discord_id text,
    p_new_summary text,
    p_message_ids_to_prune uuid[]
)
returns void language plpgsql security definer set search_path = public as $$
begin
    insert into public.conversation_summaries (context_key, owner_discord_id, summary_text, is_archived, updated_at)
    values (p_context_key, p_owner_discord_id, p_new_summary, false, now())
    on conflict (context_key) do update
    set summary_text = excluded.summary_text,
        is_archived = false,
        updated_at = now();

    update public.chat_messages
    set is_archived = true
    where context_key = p_context_key
      and id = any(p_message_ids_to_prune);
end;
$$;

-- RPC 3: Clear Memory (/memory clear) - Soft-archives messages and optional summary
create or replace function public.clear_context_memory(
    p_context_key text,
    p_wipe_summary boolean default true
)
returns integer language plpgsql security definer set search_path = public as $$
declare
    v_archived integer;
begin
    with archived as (
        update public.chat_messages
        set is_archived = true
        where context_key = p_context_key
          and is_archived = false
        returning id
    )
    select count(*) into v_archived from archived;

    if p_wipe_summary then
        update public.conversation_summaries
        set is_archived = true
        where context_key = p_context_key;
    end if;

    return v_archived;
end;
$$;

-- Lock down RPC execution to service_role only
revoke execute on function public.check_rate_limit_and_quota(text, integer) from public, anon, authenticated;
revoke execute on function public.commit_context_compression(text, text, text, uuid[]) from public, anon, authenticated;
revoke execute on function public.clear_context_memory(text, boolean) from public, anon, authenticated;

grant execute on function public.check_rate_limit_and_quota(text, integer) to service_role;
grant execute on function public.commit_context_compression(text, text, text, uuid[]) to service_role;
grant execute on function public.clear_context_memory(text, boolean) to service_role;
