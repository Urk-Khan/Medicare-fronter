import os
import sys
from pathlib import Path

# Tests never touch real services.
os.environ.update(
    SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY="", TELNYX_API_KEY="test", TELNYX_CONNECTION_ID="123",
    TELNYX_FROM_NUMBER="+18005550100", TELNYX_WEBHOOK_PUBLIC_KEY="", CARTESIA_API_KEY="test",
    OPENAI_API_KEY="test", APP_SECRET_KEY="x" * 48, PUBLIC_BASE_URL="https://example.trycloudflare.com",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
