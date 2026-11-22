# Tracebase — one container: React UI (built here) + FastAPI service.
FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# SQLCipher, for encryption at rest (TRACEBASE_DB_KEY). Prebuilt on x86-64; built from source elsewhere (ARM NAS).
RUN pip install --no-cache-dir sqlcipher3-binary || ( \
      apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libsqlcipher-dev \
      && pip install --no-cache-dir sqlcipher3 \
      && apt-get purge -y gcc libc6-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* ) \
    && python -c "import sqlcipher3"
# tree-sitter-language-pack fetches grammars on first use; bake them into the image so
# chunking works on an offline NAS. Cached outside /root: Tracebase runs as an unprivileged user.
ENV XDG_CACHE_HOME=/opt/tracebase-cache
RUN python -c "import tree_sitter_language_pack as t; \
langs = ['python','php','javascript','typescript','tsx','java','go','ruby','rust','c','cpp','csharp','kotlin','scala','swift','bash','dart','lua']; \
t.download(langs); [t.get_parser(l) for l in langs]; print('grammars cached:', len(langs))"
COPY backend/tracebase ./tracebase
COPY --from=ui /ui/dist ./ui
ENV TRACEBASE_REPOS_DIR=/volume1/repos \
    TRACEBASE_DATA_DIR=/data \
    TRACEBASE_FRONTEND_DIST=/app/ui \
    TRACEBASE_LLM_BASE_URL=http://host.docker.internal:11434
# repos are mounted read-only; git needs to trust them
RUN git config --system --add safe.directory '*'
# Runs as an unprivileged user: the entrypoint fixes /data ownership (volumes from older, root-run versions), then
# drops root. Set TRACEBASE_RUN_AS_ROOT=1 only if you need root (e.g. TRACEBASE_DOCKER_LOGS with the docker socket).
RUN groupadd --system --gid 10001 tracebase && useradd --system --uid 10001 --gid 10001 --home-dir /app tracebase \
    && mkdir -p /data && chown tracebase:tracebase /data && chmod 700 /data \
    && chown -R tracebase:tracebase /opt/tracebase-cache
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
EXPOSE 8080
VOLUME ["/data"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "tracebase.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
