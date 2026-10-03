FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

COPY proxy.py /app/proxy.py

USER nobody

EXPOSE 4000

ENTRYPOINT ["python", "-u", "/app/proxy.py"]
