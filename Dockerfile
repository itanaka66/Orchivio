FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install ".[postgres,server]"

# Run as a non-root user (DESIGN.md section 12.2).
RUN useradd --system --uid 10001 --no-create-home orchivio
USER orchivio

EXPOSE 8000
CMD ["uvicorn", "orchivio.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
