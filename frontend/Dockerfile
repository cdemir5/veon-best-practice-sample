# syntax=docker/dockerfile:1.7
# build   → Vite production build
# runtime → nginx-unprivileged (root değil, 8080), read-only root FS uyumlu
FROM node:22.12.0-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27.3-alpine AS runtime
ENV BACKEND_URL=http://backend:8000 \
    NGINX_RESOLVER=127.0.0.11 \
    GRAFANA_PUBLIC_URL=http://localhost:3000 \
    PROMETHEUS_PUBLIC_URL=http://localhost:9090 \
    KIBANA_PUBLIC_URL=http://localhost:5601 \
    JAEGER_PUBLIC_URL=http://localhost:16686
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
USER 101
