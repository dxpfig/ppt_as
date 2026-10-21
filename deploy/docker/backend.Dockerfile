FROM node:22-bookworm-slim AS build

WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY backend/package.json backend/package.json
COPY frontend/package.json frontend/package.json
COPY packages/pptd-runtime/package.json packages/pptd-runtime/package.json
RUN pnpm install --frozen-lockfile --filter backend...
COPY backend backend
RUN pnpm --filter backend build

FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    PORT=3002 \
    HOST=0.0.0.0 \
    AGENT_SERVICE_URL=http://agent:8001
WORKDIR /app
COPY --from=build /app/node_modules node_modules
COPY --from=build /app/backend/node_modules backend/node_modules
COPY --from=build /app/backend/dist backend/dist
COPY --from=build /app/backend/package.json backend/package.json
USER node
EXPOSE 3002
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=5 \
    CMD node -e "fetch('http://127.0.0.1:3002/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["node", "backend/dist/index.js"]
