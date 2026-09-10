FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for psycopg binary fallback
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY prompts ./prompts
COPY migrations ./migrations

RUN pip install --upgrade pip \
    && pip install .

# Default runtime data directory
RUN mkdir -p /app/data

EXPOSE 8080

# Default: web service; docker-compose overrides with worker command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
