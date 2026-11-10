# ---- Builder ----
FROM node:24-alpine AS builder

WORKDIR /app

# Reason: better-sqlite3 13.x ships no prebuilt binaries, so npm compiles it with node-gyp.
RUN apk add --no-cache python3 make g++

COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . ./
RUN npm run build

# Reason: prune here so the release stage gets compiled production modules without needing a toolchain.
RUN npm prune --omit=dev

# ---- Release ----
FROM node:24-alpine AS release

WORKDIR /app

COPY --from=builder /app/package.json ./
COPY --from=builder /app/package-lock.json ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/build ./build
# Local addition: admin commands for scripts/budget.sh (check, create, list).
COPY actual-admin.mjs /opt/actual-admin.mjs

ENV NODE_ENV=production
RUN TMPDIR=$(mktemp -d)
ENV TMPDIR=$TMPDIR

# Local patch: run as the image's node user (uid 1000). /data holds the budget
# cache (ACTUAL_DATA_DIR in docker-compose.yml); a new named volume mounted
# there takes this ownership.
RUN mkdir -p /data && chown node:node /data
USER node

EXPOSE 3000
# Local addition: the launcher runs build/index.js as children, one for the budget in
# /app/.env and one per connected budget under ACTUAL_ROWS_DIR (src/launcher).
ENTRYPOINT ["node", "build/launcher/main.js"]
