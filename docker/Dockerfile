# Multi-stage, multi-architecture Dockerfile for Texas Bar Enrichment & Review Console
# Compatible with linux/amd64 (Cloud Run) and linux/arm64 (Apple Silicon)

FROM ghcr.io/astral-sh/uv:0.4.18-python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install dependencies in a clean virtual environment
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy source code and install project
COPY src/ ./src/
COPY schemas/ ./schemas/
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Create a non-root system user for security
RUN groupadd -r appgroup && useradd -r -g appgroup -u 1001 appuser

# Copy virtualenv and application from builder
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appgroup /app/src /app/src
COPY --from=builder --chown=appuser:appgroup /app/schemas /app/schemas

# Create directories for data and checkpoints
RUN mkdir -p /app/data/input /app/data/output /app/data/warehouse && \
    chown -R appuser:appgroup /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    STORAGE_BACKEND=csv \
    LOCAL_WAREHOUSE_DIR=/app/data/warehouse \
    PORT=8080

USER appuser

EXPOSE 8080

# Default entrypoint supports both 'run' (Batch Job) and 'ui' (Review Dashboard)
# For Cloud Run Service: CMD ["txbar", "ui", "--host", "0.0.0.0", "--port", "8080"]
# For Cloud Run Job:     CMD ["txbar", "run"]
CMD ["txbar", "ui", "--host", "0.0.0.0", "--port", "8080"]
