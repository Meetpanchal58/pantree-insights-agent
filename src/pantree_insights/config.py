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

    # Folders that hold the business layer and the agent's memory
    semantic_dir: Path = ROOT / "semantic"
    memory_dir: Path = ROOT / "memory"

    @property
    def shop_dir(self) -> Path:
        return self.data_dir / self.data_size


@lru_cache
def get_settings() -> Settings:
    return Settings()
