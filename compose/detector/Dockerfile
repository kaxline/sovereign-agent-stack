# The detector add-on: server.py (holds the key, reaches the provider) and
# mcp_server.py (no key; check_text for Hermes). See docs/detector.md.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY detector.py server.py mcp_server.py ./

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000 8001

CMD ["python", "server.py"]
