FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core postgresql-client && rm -rf /var/lib/apt/lists/* \
 && (pip install --no-cache-dir uv==0.9.2 || pip install --no-cache-dir uv)
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
ENV PATH="/opt/venv/bin:$PATH"
CMD ["python", "-m", "app.scheduler"]
