FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

COPY search_gate.py /app/search_gate.py

# A named volume mounted here starts out owned by nobody
RUN mkdir /data && chown nobody /data

USER nobody

EXPOSE 8080

ENTRYPOINT ["python", "-u", "/app/search_gate.py"]
