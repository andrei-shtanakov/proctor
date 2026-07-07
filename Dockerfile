FROM python:3.12-slim

RUN useradd --create-home --uid 10001 proctor
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[nats]"

COPY docker/worker.yaml /etc/proctor/worker.yaml

USER proctor
ENTRYPOINT ["python", "-m", "proctor", "--config", "/etc/proctor/worker.yaml"]
