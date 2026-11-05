# syntax=docker/dockerfile:1.7
ARG PYTHON_BASE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

FROM ${PYTHON_BASE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
RUN sed -i \
      -e 's|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/20261105T000000Z|g' \
      -e 's|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/20261105T000000Z|g' \
      /etc/apt/sources.list.d/debian.sources \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.1
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY vendor/stram_data-1.3.1-py3-none-any.whl /wheels/stram_data-1.3.1-py3-none-any.whl
RUN uv export --frozen --no-dev --no-emit-project \
      --no-emit-package stram-data --no-emit-package confluent-kafka \
      --no-emit-package httpx --no-emit-package psycopg-binary \
      --no-hashes --output-file /tmp/runtime-requirements.txt \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python \
      --requirements /tmp/runtime-requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-deps \
      /wheels/stram_data-1.3.1-py3-none-any.whl . \
    && /opt/venv/bin/python -c \
      'import stram_data; assert stram_data.__version__ == "1.3.1"'

FROM ${PYTHON_BASE} AS runtime

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Stram Quant MCP" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.base.name="python:3.12.12-slim-bookworm" \
      org.opencontainers.image.base.digest="sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c" \
      io.stram.debian.snapshot="20261105T000000Z"

RUN sed -i \
      -e 's|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/20261105T000000Z|g' \
      -e 's|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/20261105T000000Z|g' \
      /etc/apt/sources.list.d/debian.sources \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && apt-get install --yes --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 stram \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin stram \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/stram-quant
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STRAM_MCP_BIND_HOST=0.0.0.0 \
    STRAM_MCP_BIND_PORT=8000 \
    STRAM_MCP_PARQUET_ROOT=/lake
USER 10001:10001
WORKDIR /nonexistent
EXPOSE 8000
ENTRYPOINT ["/opt/venv/bin/python", "-m", "stram_quant_mcp.entrypoint"]
CMD ["serve"]
