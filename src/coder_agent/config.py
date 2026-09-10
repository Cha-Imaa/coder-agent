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

    # Retrieval index (see rag/). Files above the size cap are skipped: they are generated data,
    # not code anyone reads. Chunks are sized for bge-small's 512-token window with room for the
    # path header the retriever prepends.
    index_max_file_kb: int = 512
    chunk_max_chars: int = 1_500
    chunk_window_lines: int = 60
    chunk_overlap_lines: int = 10

    # Embeddings run on the CPU through fastembed (ONNX). bge-small is 67 MB, 384 dimensions and
    # good at code for its size; the model is downloaded once into `models_dir`.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embed_batch_size: int = 64
    index_collection: str = "chunks"

    # Where local state (vector index, checkpoints) is stored, relative to the target repo.
    state_dir_name: str = ".coder-agent"

    # Run ledger (tokens, time, outcome per run). Lives in the user's home, not in the target repo,
    # because evals run across many repos and the numbers belong together.
    ledger_path: Path = Path.home() / ".coder-agent" / "ledger.sqlite"
    models_dir: Path = Path.home() / ".coder-agent" / "models"

    def state_dir(self, repo: Path) -> Path:
        return repo / self.state_dir_name

    def index_dir(self, repo: Path) -> Path:
        return self.state_dir(repo) / "chroma"


settings = Settings()
