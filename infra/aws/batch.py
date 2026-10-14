"""Run the AWS daily batch in order and save a restorable database dump."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3
import psycopg2


def run_command(command, env=None):
    if env is not None and env.get("DB_AUTH_MODE") == "iam":
        token = boto3.client("rds").generate_db_auth_token(
            DBHostname=env["DB_HOST"], Port=int(env.get("DB_PORT", "5432")), DBUsername=env["DB_USER"],
        )
        env["DB_PASSWORD"] = token
        env["PGPASSWORD"] = token
    subprocess.run(command, env=env, check=True, timeout=7200)


def wait_for_database():
    deadline = time.monotonic() + 300
    while True:
        try:
            if os.environ.get("DB_AUTH_MODE") == "iam":
                os.environ["DB_PASSWORD"] = boto3.client("rds").generate_db_auth_token(
                    DBHostname=os.environ["DB_HOST"], Port=int(os.environ.get("DB_PORT", "5432")), DBUsername=os.environ["DB_USER"],
                )
            return psycopg2.connect(
                host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
                user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
                dbname=os.environ["DB_NAME"], connect_timeout=10,
            )
        except psycopg2.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(10)


def backup(s3, bucket, env):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"backups/hecate-{stamp}.dump"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "hecate.dump"
        run_command(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(path)], env)
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    print(f"Database backup saved: s3://{bucket}/{key}", flush=True)


def run_forecast(env):
    import json
    ecs = boto3.client("ecs")
    result = ecs.run_task(
        cluster=env["ECS_CLUSTER"], taskDefinition=env["FORECAST_TASK"],
        launchType="FARGATE", networkConfiguration=json.loads(env["FORECAST_NETWORK"]),
    )
    if result.get("failures") or not result.get("tasks"):
        raise RuntimeError(f"Forecast dispatch failed: {result.get('failures')}")
    arn = result["tasks"][0]["taskArn"]
    print(f"Forecast task started: {arn}", flush=True)
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        tasks = ecs.describe_tasks(cluster=env["ECS_CLUSTER"], tasks=[arn])["tasks"]
        if tasks and tasks[0]["lastStatus"] == "STOPPED":
            if any(container.get("exitCode") != 0 for container in tasks[0]["containers"]):
                raise RuntimeError(f"Forecast task failed: {tasks[0].get('stoppedReason')}")
            print("Forecast task completed", flush=True)
            return
        time.sleep(10)
    ecs.stop_task(cluster=env["ECS_CLUSTER"], task=arn, reason="Forecast exceeded batch deadline")
    raise TimeoutError("Forecast exceeded batch deadline")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-key", help="Restore a dump from S3 into an empty warehouse")
    parser.add_argument("--backup-only", action="store_true")
    parser.add_argument("--verify", action="store_true", help="Print warehouse row counts without running jobs")
    parser.add_argument("--extras-only", action="store_true")
    args = parser.parse_args()
    connection = wait_for_database()
    env = os.environ.copy()
    for pg, db in {"PGHOST": "DB_HOST", "PGPORT": "DB_PORT", "PGUSER": "DB_USER", "PGPASSWORD": "DB_PASSWORD", "PGDATABASE": "DB_NAME"}.items():
        env[pg] = os.environ.get(db, "5432" if db == "DB_PORT" else "")
    bucket = os.environ["HECATE_EXPORT_BUCKET"]
    s3 = boto3.client("s3")
    connection.autocommit = True
    try:
        # Hold the session lock across child processes to prevent overlapping batches.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(426714790799)")
            if not cursor.fetchone()[0]:
                raise RuntimeError("Another Hecate batch is already running")
            if args.restore_key:
                cursor.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('public', 'analytics')")
                if cursor.fetchone()[0]:
                    raise RuntimeError("Restore requires an empty warehouse")
        if args.verify:
            with connection.cursor() as cursor:
                for table in ("raw_repositories", "repository_snapshots", "social_mentions", "repository_forecasts", "rag_evaluations"):
                    from psycopg2 import sql
                    cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
                    print(f"{table}: {cursor.fetchone()[0]}", flush=True)
            return 0
        if args.restore_key:
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "restore.dump")
                s3.download_file(bucket, args.restore_key, path)
                run_command(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--dbname", env["PGDATABASE"], path], env)
            print("Warehouse restored", flush=True)
            return 0
        try:
            if not args.backup_only:
                if not args.extras_only:
                    run_command([sys.executable, "-m", "pipeline.main"], env)
                    command = ["dbt", "build", "--project-dir", "/app/dbt", "--profiles-dir", "/app/dbt"]
                    if datetime.now(ZoneInfo("Asia/Singapore")).weekday() == 6:
                        command.append("--full-refresh")
                    run_command(command, env)
                failures = []
                if env.get("ENABLE_EMBEDDINGS") == "1":
                    try:
                        run_command([sys.executable, "aws_runtime.py", "pipeline.rag.embeddings"], env)
                    except subprocess.CalledProcessError:
                        failures.append("embeddings")
                if env.get("ENABLE_FORECAST") == "1":
                    try:
                        run_forecast(env)
                    except (RuntimeError, TimeoutError):
                        failures.append("forecast")
                if failures:
                    raise RuntimeError(f"Additional jobs failed: {failures}")
        finally:
            backup(s3, bucket, env)
        print("Hecate AWS batch completed", flush=True)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
