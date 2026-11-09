# IMAP mail indexer, mailbox lister and drafts outbox for the email add-on.
# Build context is the repository root:
#   docker build -f compose/email-imap/Dockerfile -t assistant-email-imap:local .
FROM python:3.11-slim

WORKDIR /app

COPY scripts/mail-index.py scripts/mail-drafts.py /app/scripts/
COPY scripts/lib/email_addon.py scripts/lib/jxa.py scripts/lib/mail_credentials.py \
     scripts/lib/mail_drafts.py scripts/lib/mail_imap.py scripts/lib/mail_imap_conn.py \
     scripts/lib/mail_imap_drafts.py scripts/lib/mail_index.py scripts/lib/mail_script.py \
     /app/scripts/lib/
COPY compose/mail-drafts-mcp/drafts.py /app/compose/mail-drafts-mcp/

# Named volumes mounted here start with this mode, so any HERMES_UID can write.
RUN mkdir -p /data/index /data/drafts && chmod 1777 /data/index /data/drafts

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Compose overrides the command: "index", "outbox" or "mailboxes --account SLUG".
CMD ["python", "-I", "-B", "/app/scripts/lib/email_addon.py", "index"]
