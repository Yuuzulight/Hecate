# Hecate on AWS

The deployed batch runs daily at **04:00 Asia/Singapore** in **ap-southeast-2 (Sydney)**. Your PC can be off.

## Deployed services

- EventBridge Scheduler starts `hecate-prod-daily` on ECS Fargate.
- One container runs extraction, dbt models and tests, refreshes vector embeddings, launches and waits for the forecast job, then creates a database backup in sequence.
- Sunday batches perform the dbt full refresh within the same job.
- Aurora PostgreSQL Express stores the warehouse. The free plan requires Express configuration, which uses IAM authentication and a TLS internet gateway rather than a private VPC database/password.
- Database capacity is 0-1 ACU and auto-pauses after 900 seconds without connections.
- The private S3 export bucket stores compressed, restorable PostgreSQL dumps.
- Valkey Serverless stores persistent repository embeddings for semantic retrieval. It uses IAM authentication and TLS, so no cache password is stored.
- A separate on-demand Fargate task runs the TimesFM forecast workload; it is invoked by the daily batch and is not an always-on service.
- Lambda and API Gateway host the public Hecate web/API endpoint. Cognito protects questions and warehouse views; the generated owner sign-in is stored only in the `hecate-prod-owner-login` Secrets Manager secret.
- A separate public-read S3 bucket (`public_snapshot_url` output) holds only `snapshot/weekly-growth.svg` and `snapshot/summary.json`; see [Public snapshot](#public-snapshot).
- Secrets Manager stores source credentials. ECS gets scoped access to the warehouse and export bucket.
- CloudWatch Logs retains job output for 30 days.
- A separate private, encrypted, versioned S3 bucket stores Terraform state with native S3 locking.

No always-on Fargate service, ALB, NAT gateway, or local scheduler is required. Embeddings, forecasts, and the protected RAG API are enabled in this deployment.

## Migration and verification

The local database was restored on 2026-10-14 and counts matched before any new collection:

| Table | Rows |
| --- | ---: |
| raw_repositories | 2,358 |
| repository_snapshots | 139,752 |
| social_mentions | 5,625 |
| repository_forecasts | 5,610 |
| rag_evaluations | 15 |

The initial AWS batch loaded 2,000 source records with no source failures, passed all 63 dbt checks, and uploaded a fresh S3 backup. New snapshots and mentions then increased the cloud warehouse counts. The old Windows daily run and local run-check tasks are disabled; local data is retained for rollback.

Migration dump: `migration/hecate-20261014.dump` in the export bucket.

## Infrastructure changes

Use the helper from the repository root. It exports temporary credentials from your named AWS login profile for Terraform's older AWS provider, without writing credentials to disk.

```powershell
aws login --profile hecate --region ap-southeast-2
.\infra\aws\terraform.ps1 -TerraformArgs @('init','-backend-config=backend.hcl')
.\infra\aws\terraform.ps1 -TerraformArgs @('plan')
.\infra\aws\terraform.ps1 -TerraformArgs @('apply')
```

`terraform.tfvars` is ignored. `backend.hcl` identifies this deployment's existing state bucket. Use different backend settings and resource names for a separate environment.

Express creation is not supported by the pinned Terraform provider: `bootstrap-database.ps1` creates the Express cluster idempotently and prepares the empty Hecate database. Terraform reads that cluster as a data source; Terraform destroy therefore does not delete the warehouse. Deletion protection is enabled on Aurora.

## Build and publish

Build from the repository root. The batch image is separate from the existing local Dockerfile.

```powershell
docker build -f infra/aws/Dockerfile -t <repository-url>:<unique-tag> .
aws ecr get-login-password --profile hecate --region ap-southeast-2 |
  docker login --username AWS --password-stdin <registry>
docker push <repository-url>:<unique-tag>
```

ECR tags are immutable. Set `pipeline_image` in the ignored tfvars file to the new tag, then apply Terraform. Application keys remain outside Terraform state; update the existing Secrets Manager secret directly. Do not include .env, dump files, state, or credentials in an image or Git.

## Run manually

```powershell
aws ecs run-task --profile hecate --region ap-southeast-2 --cluster hecate-prod --launch-type FARGATE --task-definition hecate-prod-daily --network-configuration 'awsvpcConfiguration={subnets=[subnet-03615a7e12d81cd56,subnet-03a868f8a201d64bc],securityGroups=[sg-056cf3645aac15a10],assignPublicIp=ENABLED}'
```

Task overrides can run `python aws_batch.py --backup-only` or `--verify`. To restore into an **empty** warehouse, use `--restore-key backups/<dump-name>.dump`. Restore refuses an existing warehouse and runs in one transaction.

The runner waits for Aurora to resume, refreshes IAM tokens before child processes, holds a database advisory lock to reject overlapping runs, and attempts a backup even if a pipeline/dbt stage fails. Failures return a nonzero task exit code. Scheduler retries task dispatch failures, not a running container's application failure; inspect CloudWatch and ECS stopped-task exit codes.

## Public snapshot

The last step of the daily batch (after dbt and the forecast task) runs `snapshot.py` (copied into the image as `aws_snapshot.py`). It reads the dbt marts and three row counts, then writes two files to the public bucket:

- `snapshot/weekly-growth.svg`: stars gained over 7 and 30 days, top star gainers and highest momentum across the tracked set.
- `snapshot/summary.json`: counts of repositories, snapshot days and mentions, star gains, the data date and the date generated.

Only statistics on public repositories and packages are published. Run history, job errors, mention text, secrets and anything from the Cognito-protected API are not read or written. The bucket policy allows anonymous `s3:GetObject` on `snapshot/*` only; the task role may `s3:PutObject` there and nothing else. The private exports bucket (backups) is unchanged. No credentials are needed anywhere outside AWS.

The step is fail-soft: it has a 5-minute timeout, and any error is logged as `Public snapshot skipped` without failing the batch. It is skipped when `HECATE_PUBLIC_BUCKET` is unset.

Rolling it out: build and push a new image, set `pipeline_image`, apply, then run the batch manually. Read the URLs from `terraform output public_snapshot_url`:

```text
<public_snapshot_url>/weekly-growth.svg
<public_snapshot_url>/summary.json
```

Objects are served with `Cache-Control: max-age=3600`, and GitHub proxies and caches README images, so the embedded chart is hours behind the batch, not live. The bucket sets CORS `GET *` so a site can fetch `summary.json`. If the account ever enables S3 Block Public Access at account level, the policy will be rejected at apply time.

## Costs and limits

The project is on the AWS free plan with credits; this does not make the deployment permanently free. Aurora Express is limited to 1 GB per cluster and one-day automated backup retention on this plan. Daily S3 backups retain the history independently and transition older objects to Standard-IA after 30 days.

Costs come from Fargate runtime/public IPv4, Aurora awake capacity/storage/I/O, S3, ECR image storage, Secrets Manager, and logs. Database resume and image download time are part of runtime. The earlier roughly $5 estimate is a target, not a fixed bill; actual runtime determines the monthly total.

Valkey Serverless in Sydney has a minimum storage charge of roughly US$7.37/month before requests. Embedding API usage and forecast Fargate runtime add variable costs. The public API uses `gpt-5-mini` with minimal reasoning and enforces a shared limit of 50 questions per UTC day, plus an API Gateway limit of 2 requests per second (burst 5). The UTC counter resets at 08:00 Singapore time. Scheduled embedding and forecast jobs do not use this question quota.

This is a request guardrail, not a billing hard cap. The account currently has no AWS Budget action or OpenAI project spend cap configured; add those before treating the deployment as financially bounded.

## Web access

The API is exposed through the Terraform `web_url` output. Public routes are limited to `/`, `/health`, and `/auth-config`. `/ask`, `/trending`, `/forecasts`, and `/evaluations` require a Cognito access token.

The browser page redirects to Cognito for sign-in. Retrieve the initial `owner` username and password from the `hecate-prod-owner-login` Secrets Manager secret, then change the password after the first sign-in. The browser stores only the short-lived access token; access and ID tokens expire after 15 minutes.
