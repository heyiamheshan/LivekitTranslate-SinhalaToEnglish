FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml ./
RUN uv sync --no-install-project
COPY src/ ./src/
CMD ["uv", "run", "src/agent.py", "start"]
