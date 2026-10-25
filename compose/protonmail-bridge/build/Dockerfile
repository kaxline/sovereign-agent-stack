# shenxn/protonmail-bridge-docker build/Dockerfile at 5874fde (VERSION v3.27.1),
# plus the libfido2/libcbor deps from upstream PRs #149/#150 that Bridge >= 3.22
# needs. Go builder because Bridge 3.27.1's go.mod asks for go 1.26.1.
FROM golang:1.26-trixie AS build
ARG version=v3.27.1
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config libsecret-1-dev libfido2-dev libcbor-dev
ADD https://github.com/ProtonMail/proton-bridge.git#${version} /build/
WORKDIR /build/
RUN make build-nogui vault-editor

FROM debian:trixie-slim
EXPOSE 25/tcp
EXPOSE 143/tcp
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat pass libsecret-1-0 libfido2-1 ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
COPY gpgparams entrypoint.sh /protonmail/
COPY --from=build /build/bridge /protonmail/
COPY --from=build /build/proton-bridge /protonmail/
COPY --from=build /build/vault-editor /protonmail/
ENTRYPOINT ["bash", "/protonmail/entrypoint.sh"]
