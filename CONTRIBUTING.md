# Contributing

Thank you for considering a contribution to **weeek-mcp**.

## Getting started

1. Clone the repository and install dependencies with [uv](https://docs.astral.sh/uv/).

   ```bash
   git clone https://github.com/adalekin/weeek-mcp.git
   cd weeek-mcp
   uv sync --all-extras --group dev
   ```

2. Install the Chromium runtime (only needed for knowledge base work):

   ```bash
   uv run playwright install chromium
   ```

3. Install the pre-commit hooks:

   ```bash
   uv run pre-commit install
   ```

## Running checks

Run linters, the type checker, and tests before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy weeek_mcp
uv run pytest
```

The test suite covers the task API client (mocked with `respx`), tool dispatch, and the
ProseMirror -> Markdown converter. Knowledge base access hits Weeek's internal API with a
real login, so it is exercised manually rather than in CI. When touching KB code, note the
two moving parts: the internal endpoints in `weeek_mcp/kb/client.py` and the two-step login
flow in `weeek_mcp/kb/session.py`, both validated against the live app.

## Pull requests

- Keep changes focused and describe the motivation in the PR text.
- Add or update tests when behavior changes.
- Ensure CI passes.

## Security

Please report security issues privately as described in [SECURITY.md](SECURITY.md).
