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
    model: str = "groq:openai/gpt-oss-120b"
    fallback_model: str | None = "google_genai:gemini-2.5-flash"

    # Agent loop
    max_iterations: int = 4
    max_steps: int = 40  # model calls inside `act` per run; guards against tool-call loops
    command_timeout: int = 120  # seconds, for any shell command the agent runs

    # Context management (see graph/context.py). Budget is well under gpt-oss-120b's 131k so
    # the fallback model and the tool schemas always fit too.
    context_budget_tokens: int = 60_000
    keep_recent_tool_outputs: int = 6
    tool_output_stub_chars: int = 400

    # Where local state (vector index, checkpoints) is stored, relative to the target repo.
    state_dir_name: str = ".coder-agent"

    def state_dir(self, repo: Path) -> Path:
        return repo / self.state_dir_name


settings = Settings()
