# Bitwarden broker: the bw native build plus the stdlib broker package.
# Also the image for bitwarden-setup and bitwarden-secrets-sync.
# python3-cryptography and python3-argon2 are for bitwarden-setup's local mode
# (setup/vw_register.py, setup/tls.py and the admin token).
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS bw

# Digests are GitHub's asset digests for the cli-v${BW_CLI_VERSION} release
# (gh api repos/bitwarden/clients/releases/tags/cli-v<version>). Change all
# three together.
ARG BW_CLI_VERSION=2026.10.1
ARG BW_SHA256_ARM64=79ecb3293fb259660961c949d3e32fdab8682b2bcff0f6764ce38a411ca1ac5d
ARG BW_SHA256_X64=eb09c3715291c21cf744819b873a255c1247b1b8b45bae138621a644ec7ad8f5
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# The x64 asset carries no architecture in its name.
RUN set -eu; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      arm64) asset="bw-linux-arm64-${BW_CLI_VERSION}.zip"; sum="${BW_SHA256_ARM64}" ;; \
      amd64) asset="bw-linux-${BW_CLI_VERSION}.zip"; sum="${BW_SHA256_X64}" ;; \
      *) echo "no bw build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSL -o "$asset" "https://github.com/bitwarden/clients/releases/download/cli-v${BW_CLI_VERSION}/${asset}"; \
    echo "${sum}  ${asset}" | sha256sum -c -; \
    unzip -q "$asset" bw; \
    install -m 0755 bw /usr/local/bin/bw

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

ARG BW_CLI_VERSION=2026.10.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-websockets \
       python3-cryptography python3-argon2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bw /usr/local/bin/bw /usr/local/bin/bw
RUN bw --version | tee /dev/stderr | grep -qx "${BW_CLI_VERSION}"

# A new bitwarden_sync volume copies /run/bitwarden-sync's owner and mode.
RUN groupadd --gid 10001 broker \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin broker \
    && install -d -o 10001 -g 10001 -m 0700 /run/bitwarden-sync

COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
COPY broker /app/broker
COPY secrets_sync.py /app/secrets_sync.py
COPY setup /app/setup

# The root filesystem is read-only at run time; /tmp is a tmpfs.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001

ENTRYPOINT ["/app/entrypoint.sh"]
