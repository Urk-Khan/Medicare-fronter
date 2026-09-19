-- =============================================================================
-- Medicare VoiceOps — Supabase database setup  (version 2: multiple AI agents)
-- =============================================================================
-- How to run it:
--   Supabase dashboard -> SQL Editor -> New query -> paste this whole file -> Run.
--   You should see "Success. No rows returned".
--
-- ⚠ THIS FILE REBUILDS THE DATABASE. It deletes the app's tables and creates
--   them again, so any leads, calls or settings already in this project are
--   erased. That is intentional for the upgrade to version 2 — run it once on a
--   project with no real data in it yet.
--
-- Security: Row Level Security is switched on for every table with NO public
-- policies, so the public (publishable) key can't read or write anything. Only
-- the backend, using the secret / service_role key, has access. The browser
-- never talks to Supabase directly.
-- =============================================================================

drop table if exists audit_logs        cascade;
drop table if exists import_runs       cascade;
drop table if exists connected_sheets  cascade;
drop table if exists system_config     cascade;
drop table if exists agent_script      cascade;
drop table if exists dnc_numbers       cascade;
drop table if exists callbacks         cascade;
drop table if exists transfer_attempts cascade;
drop table if exists call_events       cascade;
drop table if exists calls             cascade;
drop table if exists closers           cascade;
drop table if exists agents            cascade;
drop table if exists leads             cascade;
drop table if exists users             cascade;
drop table if exists app_schema_version cascade;

create table app_schema_version (
    version     int primary key,
    applied_at  timestamptz not null default now()
);
insert into app_schema_version (version) values (2);

-- ---------------------------------------------------------------------------
-- shared updated_at trigger
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 1. USERS (dashboard logins)
--    role: super_admin = full access (agent scripts, dialer + transfer settings,
--    user management, audit log). admin = everything else.
-- ---------------------------------------------------------------------------
create table users (
    id             uuid primary key default gen_random_uuid(),
    username       text unique not null,
    password_hash  text not null,
    full_name      text not null default '',
    role           text not null default 'admin',     -- super_admin | admin
    enabled        boolean not null default true,
    created_by     text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    last_login_at  timestamptz
);
drop trigger if exists trg_users_updated on users;
create trigger trg_users_updated before update on users for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. AI AGENTS (each one has its own voice, script, phone number and capacity)
-- ---------------------------------------------------------------------------
create table agents (
    id                    uuid primary key default gen_random_uuid(),
    name                  text not null,                  -- what the AI calls itself, e.g. Ava
    label                 text not null default '',        -- internal name for the dashboard
    enabled               boolean not null default true,
    voice_id              text not null default '',        -- Cartesia voice id (blank = the default voice)
    from_number           text not null default '',        -- Telnyx number it dials from (blank = the main number)
    max_concurrent_calls  int not null default 3,
    priority              int not null default 1,          -- lower is used first when several are free
    opening_line          text not null default '',
    system_prompt         text not null default '',
    disclaimer_text       text not null default '',
    total_calls           int not null default 0,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);
create index idx_agents_pick on agents (enabled, priority);
drop trigger if exists trg_agents_updated on agents;
create trigger trg_agents_updated before update on agents for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. LEADS
-- ---------------------------------------------------------------------------
create table leads (
    id               bigserial primary key,
    first_name       text not null default '',
    last_name        text not null default '',
    phone            text not null unique,            -- E.164, e.g. +13055550123
    phone_raw        text not null default '',
    email            text,
    zip_code         text,
    state            text,
    source           text not null default '',        -- where the lead came from
    notes            text not null default '',        -- context passed to the AI
    custom_fields    jsonb not null default '{}'::jsonb,
    timezone         text,                            -- IANA tz derived from the number
    consent_at       timestamptz,                     -- when permission to contact was given
    consent_source   text,                            -- how (web form, file attestation, ...)
    status           text not null default 'new',
        -- new | calling | retry_scheduled | callback_scheduled | awaiting_closer
        -- | transferred | contacted | not_interested | not_qualified | unreachable
        -- | wrong_number | do_not_call
    retry_count      int not null default 0,          -- total attempts made
    attempt_counts   jsonb not null default '{}'::jsonb,  -- attempts per outcome: {"no_answer": 2, "busy": 1}
    next_attempt_at  timestamptz,
    last_call_at     timestamptz,
    last_outcome     text,
    current_call_id  uuid,
    qualification    jsonb not null default '{}'::jsonb,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);
create index idx_leads_status_next on leads (status, next_attempt_at);
create index idx_leads_created on leads (created_at desc);
drop trigger if exists trg_leads_updated on leads;
create trigger trg_leads_updated before update on leads for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- 4. CLOSERS (licensed human agents calls are transferred to)
-- ---------------------------------------------------------------------------
create table closers (
    id                uuid primary key default gen_random_uuid(),
    name              text not null,
    destination       text not null default '',       -- E.164 phone or sip:user@host
    priority          int  not null default 1,        -- lower = tried first
    enabled           boolean not null default true,
    availability      text not null default 'available', -- available | away (set by humans)
    status            text not null default 'FREE',   -- FREE | RINGING | ON_CALL (set by system)
    current_call_id   uuid,
    busy_since        timestamptz,
    last_assigned_at  timestamptz,
    total_transfers   int not null default 0,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index idx_closers_pick on closers (enabled, availability, status, priority);
drop trigger if exists trg_closers_updated on closers;
create trigger trg_closers_updated before update on closers for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- 5. CALLS  (one row per phone call; the Call History page reads this)
-- ---------------------------------------------------------------------------
create table calls (
    id                       uuid primary key default gen_random_uuid(),
    lead_id                  bigint references leads(id) on delete set null,
    lead_name                text not null default '',
    phone                    text not null,
    -- a copy of the lead's details, so Call History shows everything in one query
    lead_zip                 text,
    lead_state               text,
    lead_source              text not null default '',
    agent_id                 uuid,
    agent_name               text not null default '',
    telnyx_call_control_id   text,
    telnyx_call_session_id   text,
    stream_token             text,                    -- secret in the media websocket URL
    status                   text not null default 'DIALING',
        -- DIALING | RINGING | ANSWERED | AI_CONVERSATION | TRANSFERRING | TRANSFERRED | ENDED
    ai_state                 text,                    -- greeting | qualifying | transferring | closing
    outcome                  text,
    hangup_cause             text,
    amd_result               text,
    retry_number             int not null default 0,
    is_callback              boolean not null default false,
    callback_id              uuid,
    closer_id                uuid,
    closer_name              text,
    -- set when every closer was busy: the closers work through this list themselves
    needs_closer_followup    boolean not null default false,
    followup_status          text,                    -- pending | done
    followup_by              text,
    followup_at              timestamptz,
    recording_id             text,
    recording_url            text,
    transcript               jsonb not null default '[]'::jsonb,
    qualification            jsonb not null default '{}'::jsonb,
    started_at               timestamptz not null default now(),
    answered_at              timestamptz,
    transferred_at           timestamptz,
    ended_at                 timestamptz,
    duration_seconds         int,
    talk_seconds             int,                     -- answered -> ended (what the lead actually heard)
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now()
);
create index idx_calls_ccid on calls (telnyx_call_control_id);
create index idx_calls_state on calls (status);
create index idx_calls_started on calls (started_at desc);
create index idx_calls_lead on calls (lead_id);
create index idx_calls_agent on calls (agent_id, started_at desc);
create index idx_calls_followup on calls (needs_closer_followup, followup_status);
drop trigger if exists trg_calls_updated on calls;
create trigger trg_calls_updated before update on calls for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- 6. CALL EVENTS (raw Telnyx webhooks, idempotent by event_id)
-- ---------------------------------------------------------------------------
create table call_events (
    id               uuid primary key default gen_random_uuid(),
    event_id         text unique,
    call_id          uuid,
    call_control_id  text,
    event_type       text,
    payload          jsonb not null default '{}'::jsonb,
    created_at       timestamptz not null default now()
);
create index idx_call_events_call on call_events (call_id, created_at);

-- ---------------------------------------------------------------------------
-- 7. TRANSFER ATTEMPTS (one row per closer tried for a call)
-- ---------------------------------------------------------------------------
create table transfer_attempts (
    id                uuid primary key default gen_random_uuid(),
    call_id           uuid not null references calls(id) on delete cascade,
    closer_id         uuid,
    closer_name       text not null default '',
    destination       text not null default '',
    leg_control_id    text,
    status            text not null default 'ringing',
        -- ringing | answered | connected | no_answer | busy | declined | failed | cancelled
    reason            text,
    answered_at       timestamptz,                     -- when the closer picked up
    started_at        timestamptz not null default now(),
    ended_at          timestamptz
);
create index idx_transfer_attempts_call on transfer_attempts (call_id, started_at);
create index idx_transfer_attempts_leg on transfer_attempts (leg_control_id);
create index idx_transfer_attempts_closer on transfer_attempts (closer_id, started_at desc);

-- ---------------------------------------------------------------------------
-- 8. CALLBACKS (booked when the lead asks to be called at another time)
-- ---------------------------------------------------------------------------
create table callbacks (
    id             uuid primary key default gen_random_uuid(),
    lead_id        bigint references leads(id) on delete cascade,
    call_id        uuid,
    lead_name      text not null default '',
    phone          text not null,
    reason         text not null default '',
    scheduled_for  timestamptz not null,
    status         text not null default 'pending',   -- pending | dialing | completed | cancelled
    result         text,
    created_at     timestamptz not null default now(),
    completed_at   timestamptz
);
create index idx_callbacks_status_time on callbacks (status, scheduled_for);
create index idx_callbacks_lead on callbacks (lead_id);

-- ---------------------------------------------------------------------------
-- 9. DO-NOT-CALL LIST (internal suppression, checked before every dial)
-- ---------------------------------------------------------------------------
create table dnc_numbers (
    phone       text primary key,
    reason      text not null default '',
    source      text not null default 'manual',       -- manual | upload | ai_call
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 10. SYSTEM CONFIG
--     id 'runtime'      -> the Settings page
--     id 'dialer_state' -> why automatic calling paused itself (safety brake)
-- ---------------------------------------------------------------------------
create table system_config (
    id          text primary key default 'runtime',
    config      jsonb not null default '{}'::jsonb,
    updated_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 11. GOOGLE SHEET CONNECTION + IMPORT HISTORY
-- ---------------------------------------------------------------------------
create table connected_sheets (
    id          text primary key default 'default',
    url         text not null default '',
    sheet_id    text not null default '',
    status      text not null default 'NOT CONNECTED',
    last_sync   timestamptz,
    updated_at  timestamptz not null default now()
);

create table import_runs (
    id            uuid primary key default gen_random_uuid(),
    source        text not null,            -- file | google_sheet
    name          text not null default '',
    total_rows    int not null default 0,
    imported      int not null default 0,
    duplicates    int not null default 0,
    invalid       int not null default 0,
    dnc_skipped   int not null default 0,
    created_by    text,
    created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 12. AUDIT LOG (who changed what — the super admin reads this)
-- ---------------------------------------------------------------------------
create table audit_logs (
    id          uuid primary key default gen_random_uuid(),
    username    text,
    action      text not null,
    details     jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);
create index idx_audit_created on audit_logs (created_at desc);
create index idx_audit_user on audit_logs (username, created_at desc);

-- ---------------------------------------------------------------------------
-- Row Level Security: on, with no public policies (service_role bypasses RLS)
-- ---------------------------------------------------------------------------
alter table app_schema_version enable row level security;
alter table users              enable row level security;
alter table agents             enable row level security;
alter table leads              enable row level security;
alter table closers            enable row level security;
alter table calls              enable row level security;
alter table call_events        enable row level security;
alter table transfer_attempts  enable row level security;
alter table callbacks          enable row level security;
alter table dnc_numbers        enable row level security;
alter table system_config      enable row level security;
alter table connected_sheets   enable row level security;
alter table import_runs        enable row level security;
alter table audit_logs         enable row level security;
