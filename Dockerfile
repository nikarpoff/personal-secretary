# Use uv's official Docker image with Python
FROM ghcr.io/astral-sh/uv:latest AS uv

# Use Python slim image as base
FROM python:3.12-slim

# Install uv from the uv image
COPY --from=uv /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for optimal uv behavior in Docker
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Create app directory
WORKDIR /app

# Install dependencies first: the layer survives code changes
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy the entire project
COPY . .

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Expose the port
EXPOSE 8000


# Run the application directly
ENTRYPOINT [".venv/bin/python", "-m", "uvicorn", "app.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
