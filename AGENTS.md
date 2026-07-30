# Rules

1. Follow KISS and DRY principles
2. Code should be compact and readable
3. Verify code by running `uv run task check` to check tests, linter, and types
4. Broken tests, type errors, and linter warnings must be fixed

# Stack

- Python 3.11, package manager: `uv`
- Linter: `ruff`, type checker: `pyright` (strict mode)
- Tests: `pytest` + `pytest-asyncio`

# Configuration

- Environment variables: `.env` / `.env.example`
- Model catalogue and fallback chains: `config/models.yaml`
- Agent system prompts: `config/agents/`

Never hardcode credentials, URLs, or model names.

# Testing

- `uv run task test` — run all tests
