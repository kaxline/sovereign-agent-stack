# Bitwarden sign-in signer: the pinned Hermes image, for its Chromium build and
# Python venv, plus the stdlib signer package. compose passes the same
# HERMES_AGENT_IMAGE_TAG as the hermes service, so a Hermes bump rebuilds this
# image with the agent's Chromium and user agent.
#
# The default is only for a bare docker build; compose always passes the arg.
# scripts/test-bitwarden-compose-config.py fails when it and the compose
# default drift apart, so a Hermes bump cannot leave a stale base here.
ARG HERMES_AGENT_IMAGE_TAG=v0.23.1
FROM nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG}

COPY --chown=root:root --chmod=u=rwX,go=rX signer/ /opt/bitwarden-signin/signer/
WORKDIR /opt/bitwarden-signin

# compose sets the user (HERMES_UID:HERMES_GID). No shell entrypoint: the
# signer is PID 2 under compose's init, which reaps Chromium's crashpad handlers.
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "-m", "signer"]
CMD []
