"""Settings, read from environment variables (or a local .env file)."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Data: the simulator's published shop, downloaded to DATA_DIR/<DATA_SIZE>/
    data_dir: Path = ROOT / "data"
    data_size: Literal["small", "full", "large"] = "full"

    # Query limits
    max_rows: int = 500             # most rows a query may return
    query_timeout_s: float = 30.0   # a query running longer is cancelled

    # Folders that hold the business layer, prompts and the agent's memory
    semantic_dir: Path = ROOT / "semantic"
    prompts_file: Path = ROOT / "prompts" / "agents.yaml"
    memory_dir: Path = ROOT / "memory"

    # LLM. Models are "provider:model" (LangChain init_chat_model), so OpenAI/Anthropic is a config change.
    model_main: str = "openai:gpt-6.1-terra"     # interpreter, contrast check, analyst
    model_light: str = "openai:gpt-6-luna"       # cheap jobs (e.g. rewriting the business profile)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "low"
    max_output_tokens: int = 4000
    llm_timeout_s: float = 60.0                  # a slow provider response is cut off ...
    llm_max_retries: int = 1
    max_parallel_llm_calls: int = 2  # bound concurrent provider requests per instance
    max_concurrent_runs: int = 4     # questions processed at once per instance; the rest queue visibly
    contrast_check: bool = True      # run materially different readings; ask when their numbers differ
    # Sampling temperature. None = provider default: OpenAI reasoning models (gpt-6.x) do not accept a
    # temperature, so it is only sent when set. Reports record the value used.
    temperature: float | None = None
    daily_budget_usd: float = 1.00               # live LLM calls stop for the day after this

    # Durable LangGraph checkpoints; use Postgres on Cloud Run, memory only for tests.
    checkpoint_backend: Literal["memory", "postgres"] = "memory"

    # Web app
    app_env: Literal["development", "production"] = "development"
    public_url: str = "http://127.0.0.1:8000"
    database_url: str = f"sqlite:///{ROOT / '.cache' / 'app.db'}"   # Neon Postgres in production
    session_secret: str = "dev-only-change-me"                       # signs the session cookie
    admin_emails: list[str] = []                                     # may update shared business memory
    daily_question_limit: int = 10                                   # live questions per user per day
    memory_max_age_days: int = 30          # insights older than this (vs the data's last day) are left out
    demo_dir: Path = ROOT / "demo_runs"

    # Sign-in: Google (OAuth client in Google Cloud) and/or email codes (SMTP, e.g. Gmail app password)
    google_client_id: str = ""
    google_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "Pantree Insights"

    # USD per 1M tokens: (input, cached input, output). Standard tier, OpenAI pricing page Nov 2026.
    prices: dict[str, tuple[float, float, float]] = {
        "gpt-6.1-terra": (1.00, 0.05, 5.00),
        "gpt-6-luna": (0.10, 0.01, 0.50),
        "gpt-6.1-sol": (2.00, 0.10, 10.00),
        "gpt-5.4-mini": (0.75, 0.075, 4.50),
    }

    @property
    def shop_dir(self) -> Path:
        return self.data_dir / self.data_size


@lru_cache
def get_settings() -> Settings:
    return Settings()
