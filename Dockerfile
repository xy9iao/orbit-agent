# Orbit daemon image. Runtime only: no dev extras, no build tools (all deps ship manylinux wheels).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORBIT_HOME=/app \
    ORBIT_API_HOST=0.0.0.0

WORKDIR /app

# pg_dump for the nightly export (Debian trixie ships client 17, which can dump the Postgres 16 server).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, in their own layer, so code edits don't reinstall them (stub package, then removed).
COPY pyproject.toml README.md ./
RUN mkdir orbit && touch orbit/__init__.py \
    && pip install . \
    && pip uninstall -y orbit \
    && rm -rf orbit build orbit.egg-info

# The package itself. The source tree stays in /app because alembic.ini points at orbit/storage/migrations.
COPY orbit ./orbit
RUN pip install --no-deps . && rm -rf build orbit.egg-info

COPY alembic.ini config.yaml ./
COPY persona ./persona
COPY packs ./packs
COPY evals ./evals

# UID 1000 matches the default user on most Linux hosts, so bind-mounted data/, logs/, memory/ stay writable.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin orbit \
    && mkdir -p data logs memory exports \
    && chown -R orbit:orbit data logs memory exports packs
USER orbit

# Recorded in export manifests (.git is not in the build context): GIT_COMMIT=$(git rev-parse HEAD).
ARG GIT_COMMIT=unknown
ENV ORBIT_GIT_COMMIT=$GIT_COMMIT

EXPOSE 8765
# Orbit shuts down cleanly on Ctrl+C (KeyboardInterrupt); SIGTERM would skip scheduler/channel/DB cleanup.
STOPSIGNAL SIGINT

# Explicit migration step for a clear failure in the logs; `orbit serve` also migrates (idempotent no-op then).
CMD ["sh", "-c", "alembic upgrade head && exec orbit serve"]
