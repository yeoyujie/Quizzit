FROM chainguard/python:latest-dev

ARG PYTHON_VERSION=3.14
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --python $PYTHON_VERSION

COPY src ./src

ENTRYPOINT ["uv", "run", "src/main.py"]