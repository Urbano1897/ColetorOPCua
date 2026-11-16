# One image runs either the collector or the bundled simulator.
# Compose selects the process with entrypoint / command.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin collector

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install .

USER collector

ENTRYPOINT ["opcua-collector"]
