# TianShu platform service image: two stages, no build tools at run time, no secrets baked.
#
# The console is *built here*, from the repository's own lockfile, in a stage that the runtime
# image never inherits from. Nothing the authoring machine happens to have in `apps/web/dist` is
# part of this image: the only console it can carry is the one this build produced, so an image
# cannot ship a stale or hand-made bundle and still look correct.
#
# The runtime stage is otherwise a *static* artifact: code, contracts and that one build output are
# copied in, and every deployment-specific value - database path, TLS key pair, credential
# variables - arrives at run time as a mounted file or an environment variable. Nothing here reads
# a secret at build time, so the same image can be promoted between environments without being
# rebuilt.
#
# Building this file is not the same as proving the deployment works. The exact commands that were
# actually executed, and which of them could not be executed on the authoring machine, are recorded
# in docs/platform/deployment.md and in the task handoff.

# ---- console build stage ---------------------------------------------------------------
#
# Pinned to the exact Node the repository builds with. `npm ci` installs strictly from
# package-lock.json, so the console in the runtime image is the one this lockfile describes and not
# whatever a range would have resolved to on the day of the build.
FROM node:24.19.0-bookworm-slim AS console

WORKDIR /build

ENV npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

COPY package.json package-lock.json ./
RUN npm ci

# The console's own sources, its config and its typecheck inputs. `npm run build` runs the real
# typecheck first, so a type error fails the image build instead of producing a bundle.
COPY apps/web/ ./apps/web/
RUN npm run build

# ---- runtime stage ---------------------------------------------------------------------

FROM python:3.12.11-slim-bookworm AS runtime

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
# restates a version that pyproject.toml already fixes. Only the pinned runtime dependencies and
# the product code are installed - no test tooling, no build backend left in the final layer beyond
# what pip needs, and no Node toolchain at all.
COPY pyproject.toml ./
COPY services/ ./services/
RUN python -m pip install --no-compile .

# The contracts are read-only data at run time, and the frozen diagnostic package is verified
# against its manifest hash on every readiness check. They are copied whole so no file is silently
# left behind by a pattern, and README files included because the manifest covers them. A
# deployment that keeps its own verified copy mounts it read-only over this directory; see
# docs/platform/deployment.md.
COPY contracts/ ./contracts/

# The console, from the build stage above and from nowhere else.
COPY --from=console /build/apps/web/dist/ ./web/

USER 10001:10001

# The authority store, the sidecar ledgers and the diagnostic log segments are the only state this
# process owns, and both directories are writable by the unprivileged user above.
VOLUME ["/var/lib/tianshu", "/var/log/tianshu"]

EXPOSE 8443
STOPSIGNAL SIGTERM

# Liveness over the real TLS listener, with TLS verification retained: the CA is explicit and
# hostname checking stays on, so a certificate that does not cover the URL's name fails the check
# rather than being waved through. The public liveness document is all it reads - no business
# token, no credential of any kind, and nothing written anywhere.
#
# The CA is mounted by the deployment at the path below. The default URL is the container's own
# loopback address; a deployment whose certificate does not cover `127.0.0.1` overrides the check
# with the name that certificate actually carries (see docs/platform/deployment.md).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "services.platform", "healthcheck", "--url", "https://127.0.0.1:8443/health/live", "--ca-file", "/etc/tianshu/tls/ca.pem"]

ENTRYPOINT ["python", "-m", "services.platform"]
# `serve` is the only command that binds a socket; `preflight` is run as a separate one-shot
# container before a rollout, and the local adapter is never part of a deployment.
CMD ["--settings", "/etc/tianshu/settings.json", "serve", "--host", "0.0.0.0", "--port", "8443"]
