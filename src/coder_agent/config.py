"""Central configuration.

Everything tunable lives here and is read from environment variables or a `.env` file.
Using pydantic-settings gives us typed, validated config with one line per setting,
instead of scattered `os.getenv` calls.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "provider:model" string understood by langchain's init_chat_model.
    model: str = "groq:llama-3.3-70b-versatile"
    fallback_model: str | None = "google_genai:gemini-2.5-flash"

    # Agent loop
    max_iterations: int = 4
    command_timeout: int = 120  # seconds, for any shell command the agent runs

    # Where local state (vector index, checkpoints) is stored, relative to the target repo.
    state_dir_name: str = ".coder-agent"

    def state_dir(self, repo: Path) -> Path:
        return repo / self.state_dir_name


settings = Settings()
