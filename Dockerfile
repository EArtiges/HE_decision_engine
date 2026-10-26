# Container image for Azure Container Apps (or any Docker host): serves the app on port 8000.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.4 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "app.py"]
