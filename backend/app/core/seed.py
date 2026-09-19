"""First-run setup: the super admin login, the 5 closer slots, and the first AI agent."""

from loguru import logger

from app.auth.security import seed_admin
from app.db import repo
from app.voice import script as script_mod

DEFAULT_CLOSER_COUNT = 5


async def seed_defaults() -> None:
    if not await repo.schema_ready():
        logger.error("Database tables are missing. Run backend/app/db/schema.sql in the Supabase SQL Editor, then restart.")
        return
    await seed_admin()
    if await repo.count_closers() == 0:
        for i in range(1, DEFAULT_CLOSER_COUNT + 1):
            await repo.insert_closer({
                "name": f"Licensed Agent {i}",
                "destination": "",
                "priority": i,
                "enabled": True,
                "availability": "available",
                "status": "FREE",
            })
        logger.info(f"Created {DEFAULT_CLOSER_COUNT} closer slots — add their phone numbers on the Closers page")
    if await repo.count_agents() == 0:
        await repo.insert_agent({
            "name": "Ava",
            "label": "Agent 1",
            "enabled": True,
            "voice_id": "",
            "from_number": "",
            "max_concurrent_calls": 3,
            "priority": 1,
            "opening_line": script_mod.DEFAULT_OPENING_LINE,
            "system_prompt": script_mod.DEFAULT_SYSTEM_PROMPT,
            "disclaimer_text": script_mod.DEFAULT_DISCLAIMER,
        })
        logger.info("Created the first AI agent (Ava) with the default Medicare script")
