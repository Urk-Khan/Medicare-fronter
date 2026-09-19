# Medicare VoiceOps — AI outbound calling for a Medicare campaign

Several AI voice agents call your opted-in Medicare leads, have a short, compliant qualifying
conversation, and live-transfer interested people to your licensed agents ("closers"). If a
closer is busy, the next free one is tried automatically. Everything is managed from a web
dashboard.

- **Voice AI:** Pipecat 1.8.1 — Cartesia speech
  recognition + voice, OpenAI `gpt-5.4-mini` (switchable to Claude).
- **Phone calls:** Telnyx Call Control.
- **Database:** Supabase.
- **Dashboard:** React, served by the backend at the same address.

---

## What happens on a call

0. Each **AI agent** has its own name, voice, script, phone number and "how many calls at once"
   limit. They all work the same lead list, and a lead is claimed before dialing, so two agents
   can never call the same person.
1. The dialer picks the next free agent, then the next lead that **has recorded consent**, **isn't on the do-not-call
   list**, is due (new / retry time / callback time), and is **inside calling hours in the
   lead's own time zone** (default 9 AM–8 PM, never outside 8 AM–9 PM).
2. Telnyx rings the lead. **Voicemail is detected and hung up on** (no AI voicemails).
3. When a person answers, the AI speaks straight away:
   *"Hi, this is Ava, an AI assistant calling from [your company] on a recorded line. Am I
   speaking with Mary?"* The call is **recorded**.
4. The AI explains they asked about Medicare options, checks it's a good time, reads the
   **Medicare disclaimer** word for word, then asks one question at a time:
   Part A & B? ZIP code? Current coverage? Want a licensed agent to review options?
5. **Qualified + wants help → transfer** (see below).
   Bad time → **callback booked** for the time they choose.
   "Don't call me" → **added to the do-not-call list** instantly.
   Not qualified / not interested → polite goodbye.
6. The transcript, answers, recording and every event are saved and shown on
   **Call History → Details**. The lead is updated: retry later, callback, or final status.
7. **If a call doesn't reach anybody**, it is tried again by the rules you set per reason —
   no answer, busy, voicemail and "they hung up" each get their own wait and their own number
   of tries, and you can tell the system to only try again after, say, 5 PM their time.

## How transfers work (5 closers, automatic failover)

- 5 closer slots are created on first start. Add their phone numbers on the **Closers** page.
- The AI rings the **first free closer**: enabled, marked **Available**, not already on a
  call, lowest priority number first (ties rotate to whoever had a transfer least recently).
- The closer hears *"Incoming Medicare transfer for Mary, ZIP code 3 3 1 0 1… Press 1 to
  accept."* Pressing 1 connects them to the lead. The lead waits on the line with the AI.
- **Busy, no answer, voicemail, didn't press 1, or any error → the next free closer is
  tried automatically.** Every attempt is logged.
- **Nobody available →** the AI tells the lead a licensed agent will call them back, ends the
  call politely, and puts them on the **Closer Call-Backs** page for your closers to work
  through. The dialer never calls that lead again — the closer does.
- After a successful handoff the AI steps out; the call keeps recording. When the call
  ends, the closer is marked free again.

---

## Setup (Windows, step by step)

You need: **Python 3.11** (or 3.12) and **Node.js 20+** installed.

### 1. Install everything
Double-click **`setup.bat`**. It installs the backend and builds the dashboard
(the first run takes a few minutes).

### 2. Create the database tables (one time)
This project uses its **own** Supabase project: `https://xgensexnenqrbxklmsio.supabase.co`.
1. Open that project in Supabase → **SQL Editor** → **New query**.
2. Open `backend/app/db/schema.sql` (the same file as `supabase_setup.sql`), copy everything,
   paste it in, click **Run**. You should see "Success. No rows returned".
3. Check **Table Editor**: you should see 14 tables (leads, calls, closers, callbacks…).

It's safe to run again — it never deletes data. Row Level Security is switched on for every
table, so the public (publishable) key can't read anything; only the backend can.

### 3. Check `backend/.env`
It's already filled in with:
- Cartesia + OpenAI keys and voice **from the Ashad agent**
- Telnyx and Google keys **from the old outbound project**
- `SUPABASE_URL` for the new project
- A generated admin password: look for `ADMIN_PASSWORD=` — that's your first login.

**One thing you must add** (if it isn't there already): `SUPABASE_SERVICE_ROLE_KEY`. In Supabase go to
**Project Settings → API Keys**, and copy the **secret** key (starts with `sb_secret_…`).
If you only see the older style, use the `service_role` key. Paste it after the `=`.
Don't use the *publishable* key (`sb_publishable_…`) — it's blocked from the tables on purpose.
Never put the secret key in the dashboard/frontend or share it.

### 4. Start it
Double-click **`start.bat`**. It will:
1. open a secure Cloudflare tunnel and print a `https://….trycloudflare.com` address,
2. point your Telnyx Call Control Application at that address,
3. start the backend + dashboard.

Open the printed address (or `http://localhost:8000` on the same PC) and sign in with
`admin` and the password from `.env`. Change it from the sidebar → **Password**.

> ⚠ Step 4.2 replaces the webhook URL on the Telnyx app **`Call_chk`**
> (`TELNYX_CONNECTION_ID`). It currently points at an n8n workflow. The launcher prints the
> old URL before changing it — if anything else still uses that app, give this project its
> own Call Control Application in Telnyx and put its ID in `.env`.

### 5. Before the first call — the dashboard shows a yellow checklist until these are done
1. **Settings:** company name, call-back number, and the two disclaimer numbers
   (organizations you represent / products offered).
2. **AI Agents:** one agent (Ava) is created for you. Add more if you want several calling at
   once — each can have its own voice, phone number and script.
3. **Closers:** add each licensed agent's direct phone number. Make sure their voicemail
   doesn't pick up in under ~20 seconds.
4. **Import Leads:** upload your opted-in list and choose how consent was captured.
5. **Test on yourself first:** add yourself as a lead (Leads → Add lead), click
   **Call now**, and add a second phone of yours as a closer.
6. Press **Start calling**.

## Everyday use

| Page | What it's for |
|---|---|
| Dashboard | Today or this month, all agents or one; live calls, scorecards for every agent and closer, lead pipeline |
| Call History | **Every call in one table** — live ones at the top, updating by themselves. Lead details, agent, what happened in plain words, closer, talk time, booked callback. Call now, move/cancel a callback, do-not-call. Click a row for the transcript, recording, answers and transfer attempts |
| Closer Call-Backs | Leads nobody was free for; mark them once a closer has called, or download the list as CSV |
| Leads | Search, add, edit, call now, mark do-not-call |
| Closers | Available / Away, phone numbers, priority, force-free a stuck closer |
| Do-Not-Call List | Add, upload, remove numbers |
| Import Leads | Excel/CSV or Google Sheet, column matching, consent |
| AI Agents *(super admin)* | Add agents; each one's name, voice, number, call limit and script |
| Users *(super admin)* | Who can sign in, and what they're allowed to do |
| Audit Log *(super admin)* | Who changed what, and when |
| Settings | Company details and calling hours for everyone; dialer, retries, transfers and the safety brake for the super admin |

## Who can do what

| | Super admin | Admin |
|---|---|---|
| Calls, leads, closers, do-not-call, imports | ✔ | ✔ |
| Company details and calling hours | ✔ | ✔ |
| AI agents and their scripts | ✔ | — |
| Dialer, retry, transfer and safety-brake settings | ✔ | — |
| Users and the audit log | ✔ | — |

The first login in `backend/.env` is the super admin; they add everyone else on the Users page.
These rules are enforced by the server, not just hidden in the menu.

## Safety brake

If calls keep failing — a run of failures in a row, or too many failures across the last batch —
automatic calling **pauses itself** and a red banner says why, so a bad number or a Telnyx problem
can't burn through your lead list. Both thresholds are on the Settings page.

---

## Compliance built in (not legal advice)

This campaign involves AI-voice marketing calls about Medicare, which are regulated
(TCPA, FCC rules on AI voices, CMS marketing rules, state laws). The system enforces:

- **Consent required:** leads without a recorded consent date/source are never dialed.
- **AI + company disclosure** in the first sentence, and "recorded line".
- **CMS/TPMO disclaimer** read within the first minute, and the caller can't cut it off (text editable per agent on the AI Agents page).
- **Do-not-call:** honoured instantly and permanently across every lead with that number.
- **Calling hours** in each lead's local time, capped at 8 AM–9 PM.
- **No AI voicemails**, **call recording**, full audit log.
- The AI never claims to be Medicare/the government, never asks for Medicare/Social Security/
  bank numbers, and never discusses plans, prices or benefits — that's for licensed agents.

**You're still responsible for:** getting prior express written consent that covers AI/automated
calls, scrubbing lists against the National Do Not Call Registry, the exact disclaimer numbers,
state-specific rules, and how long recordings are kept. Have your compliance contact review
the opening line and disclaimer on the AI Agents page.

---

## For developers

```
backend/
  start.py                 tunnel + Telnyx webhook + server launcher
  app/main.py              FastAPI app (API, webhooks, media WebSocket, serves dashboard)
  app/voice/bot.py         Pipecat pipeline (Ashad structure: Cartesia STT/TTS + OpenAI/Anthropic)
  app/voice/guards.py      never speaks leaked tool-call text; disclaimer can't be interrupted
  app/voice/tools.py       AI tools: save_qualification, transfer_to_licensed_agent,
                           schedule_callback, mark_do_not_call, end_call
  app/voice/script.py      default Medicare script + placeholders (each agent keeps its own copy)
  app/calls/lifecycle.py   Telnyx webhook state machine (answer, AMD, recording, hangup)
  app/calls/transfer.py    closer selection, whisper/accept, failover, bridging
  app/calls/outcomes.py    retries, callbacks, final lead statuses
  app/dialer/engine.py     dialer loop (picks a free AI agent), safety brake, maintenance sweeps
  app/core/                runtime settings, calling hours, phone numbers, background tasks, first-run seed
  app/db/                  schema.sql + all Supabase queries (repo.py)
  app/imports/             file & Google Sheet import
  tests/                   unit tests (call flows with fake DB + fake Telnyx)
frontend/src/              React dashboard
```

Run tests: `cd backend && venv\Scripts\activate && python -m pytest`

If the agent greets and then goes silent, the sentence-splitter data is missing: run
`python -m nltk.downloader punkt_tab` once (setup.bat does this; the server logs an error at
startup if it's missing).
Dashboard dev server: `cd frontend && npm run dev` (proxies `/api` to `localhost:8000`).
Real server deployment: set `PUBLIC_BASE_URL=https://your-domain`, `ENVIRONMENT=production`,
run `python start.py --no-tunnel` behind HTTPS, and set the Telnyx app webhook to
`https://your-domain/webhooks/telnyx`.
