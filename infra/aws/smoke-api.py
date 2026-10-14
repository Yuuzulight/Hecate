"""Exercise deployed authentication, retrieval, and a grounded AI answer."""

import argparse
import json
import os
import time
import boto3
import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("client")
    args = parser.parse_args()
    aws = boto3.Session(region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
    session = requests.Session()
    health = session.get(args.url + "/health", timeout=35)
    print("Health:", health.status_code)
    health.raise_for_status()
    denied = session.post(args.url + "/ask", json={"question": "What is growing?"}, timeout=35)
    print("Anonymous ask:", denied.status_code)
    assert denied.status_code == 401
    owner = json.loads(aws.client("secretsmanager").get_secret_value(SecretId="hecate-prod-owner-login")["SecretString"])
    login = aws.client("cognito-idp").initiate_auth(
        ClientId=args.client, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": owner["username"], "PASSWORD": owner["password"]},
    )
    headers = {"Authorization": "Bearer " + login["AuthenticationResult"]["AccessToken"]}
    for route in ("trending", "forecasts"):
        time.sleep(1)
        result = session.get(args.url + "/" + route, headers=headers, timeout=35)
        print(route + ":", result.status_code)
        if not result.ok:
            print(result.text[:1000])
        result.raise_for_status()
        if route == "trending":
            print("Coverage:", result.json()["coverage"])
        else:
            print("Forecast rows returned:", len(result.json()))
    time.sleep(1)
    result = session.post(args.url + "/ask", headers=headers, json={"question": "Which tracked projects are growing fastest? Cite the dataset and explain any gaps."}, timeout=35)
    print("Authenticated ask:", result.status_code)
    print(json.dumps(result.json(), ensure_ascii=True)[:2500])
    result.raise_for_status()
    assert result.json().get("answer")


if __name__ == "__main__":
    main()
