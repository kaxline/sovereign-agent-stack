# The client gateway runs on the pinned Hermes image: Starlette, uvicorn,
# websockets, aiohttp and jsonschema are already in its venv (checked on
# v0.23.1), and phase 3 imports hermes_cli.config from it.
ARG HERMES_AGENT_IMAGE_TAG=v0.23.1
FROM nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY schema /app/schema
COPY client_gateway /app/client_gateway

WORKDIR /app
EXPOSE 8650

ENTRYPOINT ["/opt/hermes/.venv/bin/python", "-m", "client_gateway.server"]
