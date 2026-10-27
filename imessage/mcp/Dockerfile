# Read-only MCP over the host-built Messages index, plus the outbox forwarder.
# Build from the imessage/ folder:  docker build -f mcp/Dockerfile imessage
FROM python:3.11-slim

WORKDIR /app

COPY mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lib/webhook.py mcp/messages_query.py mcp/forwarder.py mcp/server.py ./

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENV MESSAGES_INDEX_PATH=/data/messages/index.sqlite
ENV MESSAGES_EVENTS_DIR=/data/messages-events
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "server.py"]
