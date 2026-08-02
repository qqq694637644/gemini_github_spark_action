FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends powershell \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md SPARK_SKILL.md ./
COPY app ./app
COPY scripts ./scripts
RUN python -m pip install .

RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /data/workspaces /data/operations \
    && chown -R gateway:gateway /app /data

USER gateway
ENV WORKSPACE_ROOT=/data/workspaces \
    WORKSPACE_OPERATION_ROOT=/data/operations \
    AUDIT_DB_URL=sqlite:////data/audit.db \
    WORKSPACE_SHELL=pwsh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
