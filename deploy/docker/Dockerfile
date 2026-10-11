# One image, three roles: api | worker | scheduler (CLAUDE.md §3).
# Build from the repo root:  docker build -f deploy/docker/Dockerfile -t whatsapp-ai-agent .
# Run:  docker run --env-file .env -p 8080:8080 whatsapp-ai-agent         (API)
#       docker run --env-file .env whatsapp-ai-agent worker               (worker)
#       docker run --env-file .env whatsapp-ai-agent scheduler            (ONE scheduler)
#       docker run --env-file .env --entrypoint alembic whatsapp-ai-agent upgrade head
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY knowledge ./knowledge
# Approved template definitions: validated against TEMPLATE_* at startup.
COPY templates ./templates
# The demo clinic, for demo deployments (BUSINESS_CONFIG_PATH/FAQ_PATH point here).
COPY examples ./examples
# Ops scripts run as one-off jobs (e.g. scripts/demo_reset.py on a demo deployment).
COPY scripts ./scripts

# Run as non-root.
RUN useradd --uid 10001 --no-create-home agent
USER agent

EXPOSE 8080
ENTRYPOINT ["python", "-m", "app"]
CMD ["api"]
