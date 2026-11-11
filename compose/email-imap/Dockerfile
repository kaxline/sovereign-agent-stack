# IMAP mail indexer, mailbox lister and drafts outbox for the email add-on.
# Builds from this directory:
#   docker build -t assistant-email-imap:local compose/email-imap
FROM python:3.11-slim

WORKDIR /app

COPY mail-index.py mail-drafts.py /app/scripts/
COPY lib/ /app/scripts/lib/

# Named volumes mounted here start with this mode, so any HERMES_UID can write.
RUN mkdir -p /data/index /data/drafts && chmod 1777 /data/index /data/drafts

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Compose overrides the command: "index", "outbox" or "mailboxes --account SLUG".
CMD ["python", "-I", "-B", "/app/scripts/lib/email_addon.py", "index"]
