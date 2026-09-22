# Official linux/amd64 manifest pins; registry evidence: scripts/build/base-images.json.
# Building this definition is still pending Linux validation (TS-111).
FROM --platform=linux/amd64 node:24.19.0-bookworm-slim@sha256:e5a8dee7bc1e6a215d224a7ef8206f7e77271bc3cabd5febf2beafac0674f174 AS console
WORKDIR /build
ENV npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
# packageManager is explicit rather than inherited from the Node image.
RUN npm install --global npm@11.6.2
COPY package.json package-lock.json ./
RUN npm ci
COPY apps/web/ ./apps/web/
RUN npm run build

FROM --platform=linux/amd64 python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49 AS builder
WORKDIR /build
COPY scripts/build/tools.lock scripts/build/runtime.lock ./
RUN python -m pip install --no-cache-dir --no-deps -r tools.lock
COPY pyproject.toml ./
COPY services/ ./services/
RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels . \
    && python -m venv /opt/tianshu-venv \
    && /opt/tianshu-venv/bin/python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r runtime.lock \
    && /opt/tianshu-venv/bin/python -m pip install --no-index --no-deps /wheels/*.whl \
    && /opt/tianshu-venv/bin/python -m pip check \
    && /opt/tianshu-venv/bin/python -I -m services.platform --help

FROM --platform=linux/amd64 python:3.12.11-slim-bookworm@sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49 AS runtime
RUN groupadd --gid 10001 tianshu \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tianshu \
    && mkdir -p /srv/tianshu /var/lib/tianshu /var/log/tianshu /etc/tianshu \
    && chown 10001:10001 /var/lib/tianshu /var/log/tianshu \
    && chmod 0750 /var/lib/tianshu /var/log/tianshu
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/tianshu-venv/bin:$PATH
WORKDIR /srv/tianshu
COPY --from=builder /opt/tianshu-venv /opt/tianshu-venv
# Input must be a coordinator-verified raw-byte contract snapshot.
COPY contracts/ ./contracts/
COPY --from=console /build/apps/web/dist/ ./web/
USER 10001:10001
VOLUME ["/var/lib/tianshu", "/var/log/tianshu"]
EXPOSE 8443
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-I", "-m", "services.platform", "healthcheck", "--url", "https://127.0.0.1:8443/health/live", "--ca-file", "/etc/tianshu/tls/ca.pem"]
ENTRYPOINT ["python", "-I", "-m", "services.platform"]
CMD ["--settings", "/etc/tianshu/settings.json", "serve", "--host", "0.0.0.0", "--port", "8443"]
