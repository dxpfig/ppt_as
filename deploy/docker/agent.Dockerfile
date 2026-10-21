FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PPTD_CHROMIUM=/usr/bin/chromium \
    STUDIO_DATA_DIR=/app/runtime/studio \
    HOME=/home/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-noto-cjk \
        libreoffice-impress \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY frontend/package.json frontend/package.json
COPY backend/package.json backend/package.json
COPY packages/pptd-runtime/package.json packages/pptd-runtime/package.json
RUN pnpm install --frozen-lockfile

COPY agent_service agent_service
COPY pptd_engine pptd_engine
COPY packages packages
COPY frontend frontend
COPY backend backend
COPY docs/kimi-ppt/original docs/kimi-ppt/original
COPY scripts scripts
COPY bin bin
COPY .env.example .env.example

RUN python3 -m venv /opt/ppt-as \
    && /opt/ppt-as/bin/pip install --upgrade pip \
    && /opt/ppt-as/bin/pip install -r agent_service/requirements.txt \
    && pnpm build:pptd

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/runtime/studio \
    && chown -R app:app /app/runtime /app/agent_service /app/pptd_engine

USER app
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD /opt/ppt-as/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/studio/health', timeout=5)"
CMD ["/opt/ppt-as/bin/python", "-m", "uvicorn", "agent_service.app:app", "--host", "0.0.0.0", "--port", "8001"]
