FROM node:20-slim AS css-builder

WORKDIR /app

RUN npm install -g pnpm

# Copy all code for Tailwind content scanning
COPY . .

RUN pnpm install --frozen-lockfile
RUN pnpm run build:css

FROM ghcr.io/anomalyco/opencode:1.1.49 AS opencode

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

RUN mkdir -p /data

COPY --from=css-builder /app/staticfiles/css/output.css /app/staticfiles/css/output.css
# Tailwind source stylesheet is only needed in the builder stage.
# Keeping it in the runtime image breaks collectstatic with Manifest storage
# because it contains `@import "tailwindcss";` which Django interprets as a static reference.
RUN rm -f /app/staticfiles/css/input.css

COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode
RUN mkdir -p /root/.config/opencode && cp /app/opencode.json /root/.config/opencode/opencode.json

ENV PATH="/opt/venv/bin:$PATH"

RUN cp /app/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
