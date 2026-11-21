# Bitwarden broker: the bw native build plus the stdlib broker package.
# Also the image for bitwarden-setup.
# python3-cryptography and python3-argon2 are for bitwarden-setup's local mode
# (setup/vw_register.py, setup/tls.py and the admin token).
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS bw

# Digests are GitHub's asset digests for the cli-v${BW_CLI_VERSION} release
# (gh api repos/bitwarden/clients/releases/tags/cli-v<version>). Change all
# three together.
ARG BW_CLI_VERSION=2026.11.0
ARG BW_SHA256_ARM64=39e8336376739bbe5306471df0667793757ef1e243df3846461f66be9166e9d8
ARG BW_SHA256_X64=bfc7643cbc20f970bf04b994f96489703fcf77217a2384f1743e43848363b0ba
ARG TARGETARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# The x64 asset carries no architecture in its name. From 2026.11.0 the
# commercial-license zips (the build used before) carry a `bit` prefix.
WORKDIR /tmp
RUN set -eu; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      arm64) asset="bw-bit-linux-arm64-${BW_CLI_VERSION}.zip"; sum="${BW_SHA256_ARM64}" ;; \
      amd64) asset="bw-bit-linux-${BW_CLI_VERSION}.zip"; sum="${BW_SHA256_X64}" ;; \
      *) echo "no bw build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o "$asset" "https://github.com/bitwarden/clients/releases/download/cli-v${BW_CLI_VERSION}/${asset}"; \
    echo "${sum}  ${asset}" | sha256sum -c -; \
    unzip -q "$asset" bw; \
    install -m 0755 bw /usr/local/bin/bw

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

ARG BW_CLI_VERSION=2026.11.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-websockets \
       python3-cryptography python3-argon2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bw /usr/local/bin/bw /usr/local/bin/bw
RUN v="$(bw --version)"; echo "$v" >&2; test "$v" = "${BW_CLI_VERSION}"

COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
COPY broker /app/broker
COPY setup /app/setup

# The root filesystem is read-only at run time; /tmp and /run/bitwarden-sync
# are tmpfs mounts. Compose runs both services as HERMES_UID:HERMES_GID, which
# need no passwd entry: HOME is /tmp.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 1000:1000

ENTRYPOINT ["/app/entrypoint.sh"]
