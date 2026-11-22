# The SSH listener image (docs/ssh-listener.md). The release workflow builds it
# for linux/amd64 and linux/arm64, pushes it to GHCR, and pins it by digest in
# that release's ssh-listener.sh. `make ssh-listener-image` builds it locally.
#
# Debian rather than Alpine: udx-native ships no musl prebuild.
FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --omit=dev
COPY lib.mjs server.mjs listener.mjs ./

# `make ssh-listener-test` and scripts/test-ssh-listener.sh build this stage,
# which runs the tests against a local testnet. Nothing else uses it. It adds
# OpenSSH's sshd and the ssh2 client (a dev dependency) for
# test/sshd.test.mjs; the release stage copies /app from build, without them.
FROM build AS test
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd
RUN --mount=type=cache,target=/root/.npm npm ci
COPY test ./test
RUN node --test test/*.test.mjs

FROM node:24-bookworm-slim AS release
ARG VERSION=dev
ENV NODE_ENV=production SSH_LISTENER_VERSION=${VERSION} SSH_LISTENER_DATA=/data
WORKDIR /app
COPY --from=build /app ./
# A new named volume copies this folder's owner, so the node user can write it.
RUN mkdir /data && chown node:node /data
VOLUME /data
USER node
ENTRYPOINT ["node", "/app/listener.mjs"]
CMD ["serve"]
LABEL org.opencontainers.image.source="https://github.com/kaxline/sovereign-agent-stack" \
      org.opencontainers.image.description="Passes HyperDHT streams from allowed client keys to the host's sshd" \
      org.opencontainers.image.licenses="MIT"
