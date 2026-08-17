FROM python:3.12-slim AS base

WORKDIR /app

# Pull current OS security patches at build time rather than trust the base
# image snapshot's age -- Debian ships fixes faster than upstream base
# images get rebuilt, and Trivy (in CI) gates on exactly this gap.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
