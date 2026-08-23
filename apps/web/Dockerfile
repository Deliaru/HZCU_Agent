FROM node:24-alpine AS dependencies
WORKDIR /app
ARG NPM_CONFIG_REGISTRY=https://mirrors.tencent.com/npm/
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund --registry="$NPM_CONFIG_REGISTRY"

FROM node:24-alpine AS builder
WORKDIR /app
ARG NEXT_PUBLIC_API_BASE_URL=/api/v1
ARG HZCU_API_INTERNAL_URL=http://api:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV HZCU_API_INTERNAL_URL=$HZCU_API_INTERNAL_URL
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
