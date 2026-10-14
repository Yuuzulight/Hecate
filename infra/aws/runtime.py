"""Resolve short-lived cloud credentials for each job or API invocation."""

import json
import os
import sys
from urllib.parse import quote

import boto3


def configure_environment():
    if os.environ.get("API_SECRET_ARN"):
        value = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["API_SECRET_ARN"])
        for key, secret in json.loads(value["SecretString"]).items():
            os.environ[key.upper()] = secret
    if os.environ.get("DB_AUTH_MODE") == "iam":
        os.environ["DB_PASSWORD"] = boto3.client("rds").generate_db_auth_token(
            DBHostname=os.environ["DB_HOST"], Port=int(os.environ.get("DB_PORT", "5432")), DBUsername=os.environ["DB_USER"],
        )
    if os.environ.get("VALKEY_HOST"):
        from developer_toolkit_elasticache import generate_iam_auth_token
        username = "default.iam-user"
        token = generate_iam_auth_token(
            serverless_cache_name=os.environ["VALKEY_NAME"], user_id=username,
            region=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION")),
        )
        os.environ["REDIS_URL"] = f"rediss://{username}:{quote(token, safe='')}@{os.environ['VALKEY_HOST']}:6379/0"
        os.environ["REDIS_PROTOCOL"] = "2"


def main():
    import runpy
    configure_environment()
    runpy.run_module(sys.argv[1], run_name="__main__")


if __name__ == "__main__":
    main()
