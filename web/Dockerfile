FROM node:20-slim AS css-builder

WORKDIR /app

RUN npm install -g pnpm

# Copy all code for Tailwind content scanning
COPY . .

RUN pnpm install --frozen-lockfile
RUN pnpm run build:css

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

COPY --from=css-builder /app/staticfiles/css/output.css /app/staticfiles/css/output.css

ENV PATH="/opt/venv/bin:$PATH"
