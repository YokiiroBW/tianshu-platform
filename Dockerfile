# TianShu platform service image: one Python process, no build tools at run time, no secrets baked.
#
# The image is a *static* artifact: code, contracts and the built console are copied in, and every
# deployment-specific value - database path, TLS key pair, credential variables - arrives at run
# time as a mounted file or an environment variable. Nothing here reads a secret at build time, so
# the same image can be promoted between environments without being rebuilt.
#
# Building this file is not the same as proving the deployment works. The exact commands that were
# actually executed, and which of them could not be executed on the authoring machine, are recorded
# in docs/platform/deployment.md and in the task handoff.

FROM python:3.12.11-slim-bookworm

# A service that can rewrite its own code cannot be trusted to report what it did, and uid 0 inside
# a container is uid 0 on the host that shares its kernel.
RUN groupadd --gid 10001 tianshu \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tianshu \
    && mkdir -p /srv/tianshu /var/lib/tianshu /var/log/tianshu /etc/tianshu \
    && chown -R 10001:10001 /var/lib/tianshu /var/log/tianshu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/tianshu

# The project's own metadata is the single source of pinned runtime dependencies: the image never
# restates a version that pyproject.toml already fixes.
COPY pyproject.toml ./
COPY services/ ./services/
RUN python -m pip install --no-compile .

# The contracts are read-only data at run time, and the frozen diagnostic package is verified
# against its manifest hash on every readiness check. They are copied whole so no file is silently
# left behind by a pattern, and README files included because the manifest covers them.
COPY contracts/ ./contracts/

# The built single-page console. `apps/web/dist` is produced outside the image by the repository's
# own build; a missing build fails this step loudly instead of shipping a console that cannot load.
COPY apps/web/dist/ ./web/

USER 10001:10001

# The authority store, the sidecar ledgers and the diagnostic log segments are the only state this
# process owns, and both directories are writable by the unprivileged user above.
VOLUME ["/var/lib/tianshu", "/var/log/tianshu"]

EXPOSE 8443
STOPSIGNAL SIGTERM

# Liveness over the real TLS listener, verified against the mounted certificate: an unverified
# probe would report a healthy process even when the entry point clients actually use is broken.
# Only the liveness document is read, so the check needs no credential and reveals no dependency.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import ssl, urllib.request; context = ssl.create_default_context(cafile='/etc/tianshu/tls/server.pem'); urllib.request.urlopen('https://127.0.0.1:8443/health/live', context=context, timeout=2)"]

ENTRYPOINT ["python", "-m", "services.platform"]
# `serve` is the only command that binds a socket; `preflight` is run as a separate one-shot
# container before a rollout, and the local adapter is never part of a deployment.
CMD ["--settings", "/etc/tianshu/settings.json", "serve", "--host", "0.0.0.0", "--port", "8443"]
