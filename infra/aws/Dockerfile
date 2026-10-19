FROM python:3.11-slim-trixie

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PGSSLMODE=require
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-rag.txt ./
RUN pip install --no-cache-dir -r requirements-rag.txt dbt-core==1.12.0 dbt-postgres==1.11.0 boto3==1.43.113 developer-toolkit-elasticache
RUN useradd --create-home --uid 1000 hecate
COPY pipeline/ ./pipeline/
COPY dbt/ ./dbt/
COPY infra/aws/batch.py ./aws_batch.py
COPY infra/aws/runtime.py ./aws_runtime.py
COPY infra/aws/snapshot.py ./aws_snapshot.py
RUN chown -R hecate:hecate /app
USER hecate
CMD ["python", "aws_batch.py"]
