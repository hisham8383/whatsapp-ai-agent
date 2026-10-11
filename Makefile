# Keep in sync with CLAUDE.md §11 and tasks.ps1 (Windows equivalent).
PY ?= python
IMAGE ?= whatsapp-ai-agent:dev

.PHONY: dev worker scheduler test lint eval eval-demo migrate build

dev:        ## run the API with auto-reload
	$(PY) -m uvicorn app.main:create_app --factory --reload --no-access-log --port 8080

worker:     ## run the job loop
	$(PY) -m app worker

scheduler:  ## run the scheduler (reminders, follow-ups, alerts, sync, retention); ONE only
	$(PY) -m app scheduler

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check app tests evals scripts alembic
	$(PY) -m ruff format --check app tests evals scripts alembic
	$(PY) -m mypy app tests evals scripts

eval:       ## delivery gate: this client's evals/cases.yaml, 3 reps (calls the paid API)
	$(PY) -m evals.runner

eval-demo:  ## the reference run on the demo clinic (about $1.20)
	$(PY) -m evals.runner --cases examples/demo-clinic/eval-cases.yaml

migrate:
	$(PY) -m alembic upgrade head

build:
	docker build -f deploy/docker/Dockerfile -t $(IMAGE) .
