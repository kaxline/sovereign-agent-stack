# Draft-only mail MCP: queues requests for the host helper. See docs/email.md.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY drafts.py server.py ./

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENV MAIL_DRAFTS_PATH=/data/mail-drafts
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "server.py"]
