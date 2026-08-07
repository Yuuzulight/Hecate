# Build:  docker build -t hecate:v1 .
# Run:    docker run --rm --network hecate_default -e DB_HOST=postgres \
#                    -e DB_PASSWORD=dataflow hecate:v1
#
# There is a second image here for dbt:
#
#   docker build --target dbt -t hecate-dbt:v1 .
#
# Same repository, same commit, so the models can never drift from the pipeline
# that produced the data they read. It is a separate image rather than a bigger
# one because dbt is a tool that runs against the database, and the pipeline has
# no use for it - carrying it would be ~100MB the daily job never executes.

# - Dependencies are built in a throwaway stage so the compilers and pip caches
#   they drag in never reach the image that ships.
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# - Sits before the pipeline stage deliberately. Docker builds the last stage
#   when no target is given, and `docker build -t hecate:v1 .` documented above
#   has to keep producing the pipeline.
FROM python:3.11-slim AS dbt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/home/hecate/.local/bin:$PATH \
    DBT_PROFILES_DIR=/app/dbt

RUN useradd --create-home --uid 1000 hecate

USER hecate
RUN pip install --user --no-cache-dir dbt-core==1.12.0 dbt-postgres==1.11.0

WORKDIR /app
COPY --chown=hecate:hecate dbt/ ./dbt/

# - Fails the job on a failing test rather than leaving bad marts in place
#   looking authoritative. `dbt build` runs models and their tests together, so
#   a model whose test fails does not get handed to the dashboard.
CMD ["dbt", "build", "--project-dir", "/app/dbt", "--profiles-dir", "/app/dbt"]


FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/home/hecate/.local/bin:$PATH

# - Runs as a normal user. Nothing here needs root, and a pipeline that talks to
#   four external APIs is not the place to hand it out for free.
RUN useradd --create-home --uid 1000 hecate

COPY --from=builder --chown=hecate:hecate /root/.local /home/hecate/.local

WORKDIR /app
COPY --chown=hecate:hecate pipeline/ ./pipeline/

USER hecate

# - Only meaningful for the long-running deployment. The scheduled job runs once
#   and exits, and a container that has finished has nothing left to check.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "from pipeline.config import Config; from pipeline.loader import PostgreSQLLoader; loader = PostgreSQLLoader(Config()); loader.connect(); loader.close()" || exit 1

CMD ["python", "-m", "pipeline.main"]
