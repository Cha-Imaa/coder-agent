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

    # Resilience (see llm.py). `llm_sdk_retries` is the provider SDK's own retry count (429/5xx
    # with Retry-After); `llm_attempts` is how many times the primary is tried at LangChain level
    # before the fallback model gets the call; the wait between attempts grows from
    # `llm_retry_initial_seconds` with jitter.
    llm_sdk_retries: int = 2
    llm_attempts: int = 2
    llm_retry_initial_seconds: float = 1.0

    # Agent loop
    max_iterations: int = 4
    max_steps: int = 40  # model calls inside `act` per run; guards against tool-call loops
    command_timeout: int = 120  # seconds, for any shell command the agent runs

    # Where shell commands run (see sandbox/). `local` is the host behind a denylist; `docker` is
    # a fresh container per command with the repo as its only mount. The MCP server is a separate
    # process and reads these from its environment, so the client mirrors them into it.
    sandbox_mode: str = "local"
    sandbox_image: str = "coder-sandbox"  # built from sandbox/Dockerfile on first use
    sandbox_network: str = "none"
    sandbox_memory: str = "1g"
    sandbox_cpus: float = 1.0

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

    # Embeddings run on the CPU through fastembed (ONNX). arctic-embed-xs is 22M parameters,
    # 384 dimensions, a 512-token window, and ships as float32: the int8-quantised bge-small that
    # fastembed serves ran 14x slower on a laptop CPU without VNNI. Downloaded once to `models_dir`.
    embedding_model: str = "snowflake/snowflake-arctic-embed-xs"
    # Instruction the model was trained to see in front of a query (not in front of documents).
    # This exact string is the one arctic-embed and bge-en-v1.5 both use.
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embed_batch_size: int = 64
    index_collection: str = "chunks"

    # Retrieval (see rag/retriever.py and graph/retrieval.py). `retrieval_mode` is what the
    # ablation flips: hybrid | dense | bm25 | off. `candidates` is how many hits each side
    # contributes before rank fusion picks the final `retrieval_k`, and `context_chars` caps what
    # the planner is shown.
    retrieval_mode: str = "hybrid"
    retrieval_candidates: int = 20
    retrieval_k: int = 6
    retrieval_context_chars: int = 6_000

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
