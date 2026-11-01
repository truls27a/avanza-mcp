FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy AVANZA_MCP_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && useradd --system --uid 10001 --no-create-home avanza \
    && install -d -o avanza -g avanza -m 700 /data
USER avanza
VOLUME /data
EXPOSE 8000
# Binding 0.0.0.0 requires AVANZA_MCP_TOKEN (or --allow-unauthenticated behind a private tunnel).
ENTRYPOINT ["/app/.venv/bin/avanza-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
