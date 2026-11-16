# Contributing to txbar

Thank you for your interest in contributing to the Texas Bar Attorney Enrichment & Entity Resolution Pipeline!

## Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/zebok/attorney-scrapper.git
   cd attorney-scrapper
   ```

2. **Install dependencies:**
   We recommend [uv](https://docs.astral.sh/uv/):
   ```bash
   uv sync --all-groups
   ```
   Or standard `pip` with Python 3.12:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   ```

## Running Checks & Tests

Before submitting code, ensure that all linting and test passes succeed:

```bash
# Run tests
uv run pytest -v

# Check linting
uv run ruff check .

# Check formatting
uv run ruff format --check .
```

## Adding a New Directory Provider

To support another State Bar (e.g., California, New York, Florida):
1. Implement the `DirectoryProvider` protocol defined in `src/txbar/provider.py`.
2. Add declarative nickname, firm alias, or metro rules in `data/rules/*.json` if applicable.
3. Write unit tests against saved HTML fixtures verifying profile extraction and DOM drift detection.

## Pull Requests

1. Create a feature branch: `git checkout -b feature/my-feature`.
2. Commit your changes with clear, descriptive commit messages.
3. Push to your fork and submit a Pull Request to `main`.
