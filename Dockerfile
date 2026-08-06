# Build:  docker build -t hecate:v1 .
# Run:    docker run --rm --network hecate_default -e DB_HOST=postgres \
#                    -e DB_PASSWORD=dataflow hecate:v1

# - Dependencies are built in a throwaway stage so the compilers and pip caches
#   they drag in never reach the image that ships.
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


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
    CMD python -c "from pipeline.config import Config; from pipeline.loaders import PostgreSQLLoader; loader = PostgreSQLLoader(Config()); loader.connect(); loader.close()" || exit 1

CMD ["python", "-m", "pipeline.main"]
