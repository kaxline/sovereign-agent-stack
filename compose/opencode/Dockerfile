# OpenCode image + git. Upstream ships no git binary, so `git rev-parse`
# worktree detection fails and every directory resolves to the `global` project.
# Pinned: latest moves under a rebuild. Override with --build-arg.
ARG OPENCODE_VERSION=1.19.6
FROM ghcr.io/anomalyco/opencode:${OPENCODE_VERSION}

# safe.directory pre-empts git's "dubious ownership" refusal on bind-mounted
# repos whose uid does not match the user OpenCode runs as. --system, because
# that user is chosen at start (entrypoint.sh), not here.
RUN apk add --no-cache git openssh-client ca-certificates su-exec \
 && git config --system --add safe.directory '*'

# HOME outside /root, which only root can enter.
ENV HOME=/home/opencode
COPY entrypoint.sh /usr/local/bin/opencode-entrypoint
RUN chmod 755 /usr/local/bin/opencode-entrypoint
ENTRYPOINT ["/usr/local/bin/opencode-entrypoint"]
