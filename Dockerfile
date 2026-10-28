FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/code

WORKDIR /code

# Build dependencies for cryptography/bcrypt wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /code/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Source only: .dockerignore keeps .env, keys, .venv and tests out of the image.
# Secrets arrive at runtime (env_file + a read-only keys mount, see docker-compose.yml).
COPY app /code/app

# Run as an unprivileged user
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
