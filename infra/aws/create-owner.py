"""Create the initial app owner and store credentials in Secrets Manager."""

import argparse
import json
import secrets
import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pool")
    parser.add_argument("url")
    args = parser.parse_args()
    cognito = boto3.client("cognito-idp")
    manager = boto3.client("secretsmanager")
    try:
        cognito.admin_get_user(UserPoolId=args.pool, Username="owner")
        print("Owner already exists; credentials were preserved.")
        return
    except cognito.exceptions.UserNotFoundException:
        pass
    password = "Aa1!" + secrets.token_urlsafe(24)
    name = "hecate-prod-owner-login"
    value = json.dumps({"username": "owner", "password": password, "url": args.url})
    try:
        manager.create_secret(Name=name, SecretString=value, Tags=[{"Key": "Project", "Value": "hecate"}])
    except manager.exceptions.ResourceExistsException:
        manager.put_secret_value(SecretId=name, SecretString=value)
    cognito.admin_create_user(UserPoolId=args.pool, Username="owner", TemporaryPassword=password, MessageAction="SUPPRESS")
    cognito.admin_set_user_password(UserPoolId=args.pool, Username="owner", Password=password, Permanent=True)
    print("Owner created. Credentials are in Secrets Manager: " + name)


if __name__ == "__main__":
    main()
