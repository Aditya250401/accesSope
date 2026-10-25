"""Load local configuration without putting credentials in graph state or logs."""
import os
from pathlib import Path

from dotenv import load_dotenv


def load_settings():
    # Explicit exported variables take precedence over the ignored local .env.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    # Current LangSmith SDKs treat AGENT_ID/ENVIRONMENT as a registered deployed
    # agent destination. Local project tracing is a different destination; setting
    # both is ambiguous. Retain legacy labels as metadata, not a second address.
    legacy_id = os.environ.pop("LANGSMITH_AGENT_ID", None)
    legacy_environment = os.environ.pop("LANGSMITH_AGENT_ENVIRONMENT", None)
    if legacy_id:
        os.environ.setdefault("ACCESSSCOPE_AGENT_LABEL", legacy_id)
    if legacy_environment:
        os.environ.setdefault("ACCESSSCOPE_ENVIRONMENT", legacy_environment)
    os.environ.setdefault("LANGSMITH_PROJECT", "accessscope")
    return os.environ.get("OPENAI_MODEL") or os.environ.get("CHAT_MODEL", "gpt-4o")


def require_agent_credentials():
    missing = [name for name in ("OPENAI_API_KEY", "TAVILY_API_KEY") if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing local configuration: " + ", ".join(missing))
