"""Central configuration.

Everything tunable lives here and is read from environment variables or a `.env` file.
Using pydantic-settings gives us typed, validated config with one line per setting,
instead of scattered `os.getenv` calls.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def provider_of(model: str) -> str:
    """The provider half of a "provider:model" string ("ollama:qwen2.5-coder:7b" -> "ollama")."""
    return model.split(":", 1)[0] if ":" in model else ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CODER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "provider:model" string understood by langchain's init_chat_model. Four providers are
    # wired: `groq:` and `google_genai:` (hosted, free tiers), `k2think:` (MBZUAI's hosted
    # reasoning model behind an OpenAI-compatible endpoint, see llm.py) and `ollama:` (a model
    # served by a local daemon, e.g. `ollama:qwen2.5-coder:7b`; the model's own tag keeps its colon).
    model: str = "groq:openai/gpt-oss-120b"
    fallback_model: str | None = "google_genai:gemini-2.5-flash"

    # K2 Think (see llm.py). The endpoint speaks the OpenAI chat-completions dialect, so the
    # `openai` integration is reused with a different base URL; the key is `K2_API_KEY` in the
    # environment, read the way the other SDKs read theirs, not a `CODER_` setting.
    # `reasoning_effort` is how much the model thinks before answering; its thinking counts as
    # output tokens and sits inside the same 64k window as the prompt.
    k2_base_url: str = "https://api.k2think.ai/v1"
    k2_reasoning_effort: str = "medium"

    # Local models (see llm.py). Ollama allocates `ollama_num_ctx` tokens of context per request;
    # its own default is 2,048, which the tool schemas plus one file read exceed, and the daemon
    # then drops the oldest messages silently rather than erroring. 16k covers the trimmed
    # conversation the graph sends when `context_budget_tokens` is lowered to match (a 7B model
    # on a laptop CPU has no room for the 60k budget the hosted models get).
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_num_ctx: int = 16_384

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
    # the fallback model and the tool schemas always fit too. Providers whose endpoint rejects
    # a request over a hard window (prompt plus completion) are listed with that window; the
    # budget is then capped at the window minus a reserve for what the approximate token count
    # does not see: the tool schemas, the system prompt, and the reply, which for a reasoning
    # model includes everything it thinks before it answers.
    context_budget_tokens: int = 60_000
    context_windows: dict[str, int] = {"k2think": 65_536}
    context_reserve_tokens: int = 24_000
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

    # Optional second stage (see rag/reranker.py): a cross-encoder re-scores the top
    # `rerank_candidates` chunks from the first stage and the best `retrieval_k` go to the
    # planner. Off by default: it adds about 1.5 s per query on a laptop CPU and its value is
    # what the retrieval eval measures.
    rerank: bool = False
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_candidates: int = 20

    # Where local state (vector index, checkpoints) is stored, relative to the target repo.
    state_dir_name: str = ".coder-agent"

    # Run ledger (tokens, time, outcome per run). Lives in the user's home, not in the target repo,
    # because evals run across many repos and the numbers belong together.
    ledger_path: Path = Path.home() / ".coder-agent" / "ledger.sqlite"
    models_dir: Path = Path.home() / ".coder-agent" / "models"
    # `coder fix-issue <url>` without --repo clones the repository here, one directory per
    # owner/name, and fetches into it on later runs. GitHub credentials are not settings: the
    # GitHub tool server reads the standard GITHUB_TOKEN from its environment.
    checkouts_dir: Path = Path.home() / ".coder-agent" / "checkouts"

    def context_budget(self, model: str | None = None) -> int:
        """Tokens of conversation the graph may send to `model` (the primary by default)."""
        window = self.context_windows.get(provider_of(model or self.model))
        if window is None:
            return self.context_budget_tokens
        return min(self.context_budget_tokens, window - self.context_reserve_tokens)

    def state_dir(self, repo: Path) -> Path:
        return repo / self.state_dir_name

    def index_dir(self, repo: Path) -> Path:
        return self.state_dir(repo) / "chroma"


settings = Settings()
