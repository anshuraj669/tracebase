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
# tree-sitter-language-pack fetches grammars on first use; bake them into the image so
# chunking works on an offline NAS.
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
EXPOSE 8080
VOLUME ["/data"]
CMD ["uvicorn", "tracebase.main:app", "--host", "0.0.0.0", "--port", "8080"]
