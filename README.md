# coder-agent

A terminal coding agent that takes a task in plain English, reads a target repository, plans a change,
edits files, runs the tests, and iterates on failures until they pass.

Built to learn the agentic-AI stack end to end, on a zero-cost setup:

| Concern | Technology |
|---|---|
| Agent orchestration | LangGraph state graph (plan → act → test → reflect) |
| Tools | Model Context Protocol (MCP) server exposing file and shell tools |
| Codebase retrieval | Local embeddings (bge-small) + Chroma vector store |
| LLM | Groq free tier, Gemini free tier as fallback, swappable via one env var |
| Observability | LangSmith tracing |
| Interface | Typer + Rich CLI |

## Status

Work in progress. See the commit history for the step-by-step build.

## Setup

```bash
uv sync --extra dev
cp .env.example .env   # fill in your keys
```
