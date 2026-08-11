# OpenCode image + git. Upstream ships no git binary, so `git rev-parse`
# worktree detection fails and every directory resolves to the `global` project.
FROM ghcr.io/anomalyco/opencode:latest

# safe.directory pre-empts git's "dubious ownership" refusal on bind-mounted
# repos whose uid does not match the container's root.
RUN apk add --no-cache git openssh-client ca-certificates \
 && git config --global --add safe.directory '*'
