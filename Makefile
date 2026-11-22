.PHONY: setup demo dev ui test docker cli

setup:            ## install backend + frontend deps
	cd backend && uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt pytest
	-cd backend && uv pip install -p .venv/bin/python sqlcipher3-binary   # optional: encryption at rest (TRACEBASE_DB_KEY)
	cd frontend && npm install
	cd cli && uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e . pytest

cli:              ## install the `tracebase` command for your user (copy + MCP server)
	uv tool install --force --editable ./cli

demo:             ## generate demo repos in demo/volume1/repos
	python3 demo/seed.py --reset

ui:               ## build the React UI (served by FastAPI)
	cd frontend && npm run build

dev: ui           ## run Tracebase on http://localhost:8080 against the demo repos
	cd backend && TRACEBASE_REPOS_DIR=../demo/volume1/repos TRACEBASE_LOGS_DIR=../demo/volume1/logs TRACEBASE_DATA_DIR=../demo/volume1/tracebase-data \
		.venv/bin/python -m uvicorn tracebase.main:app --port 8080 --reload

test:
	cd backend && .venv/bin/python -m pytest -q
	cd cli && .venv/bin/python -m pytest -q tests

docker:
	docker compose up --build
