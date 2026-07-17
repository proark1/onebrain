FROM node:22-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build && npm prune --omit=dev

FROM node:22-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

WORKDIR /app

RUN groupadd --system --gid 10001 onebrain \
    && useradd --system --uid 10001 --gid onebrain --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin onebrain

COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/next.config.ts ./next.config.ts
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next

RUN install -d --owner=onebrain --group=onebrain --mode=0700 /tmp/onebrain /app/.next/cache \
    && chown --recursive onebrain:onebrain /app/.next/cache

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOME=/tmp/onebrain \
    TMPDIR=/tmp/onebrain \
    XDG_CACHE_HOME=/tmp/onebrain/cache \
    NPM_CONFIG_CACHE=/tmp/onebrain/npm-cache

USER onebrain
EXPOSE 3000

CMD ["npm", "run", "start"]
