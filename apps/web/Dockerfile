# Production web: static SPA + same-origin reverse proxy to the API (ADR-0004/0009).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
# API_UPSTREAM: where /v1 is proxied. BLOB_ORIGIN: storage origin the browser uploads to (CSP).
ENV API_UPSTREAM=http://api:8000 BLOB_ORIGIN=""
COPY deploy/*.template /etc/nginx/templates/
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
