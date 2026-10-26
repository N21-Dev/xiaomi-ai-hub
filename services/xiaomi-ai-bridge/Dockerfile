ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" . \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --no-create-home --shell /usr/sbin/nologin bridge \
    && mkdir /data && chown bridge:bridge /data
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "xiaomi_ai_bridge"]
