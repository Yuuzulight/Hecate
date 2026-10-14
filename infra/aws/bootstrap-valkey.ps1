[CmdletBinding()]
param([string]$Profile = 'hecate', [string]$Region = 'ap-southeast-2')
$ErrorActionPreference = 'Stop'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$result = & $aws elasticache describe-serverless-caches --serverless-cache-name hecate-prod-vectors --profile $Profile --region $Region 2>&1
if ($LASTEXITCODE -ne 0) {
    if (($result -join ' ') -notmatch 'ServerlessCacheNotFoundFault') { throw ($result -join ' ') }
    & $aws elasticache create-serverless-cache --serverless-cache-name hecate-prod-vectors --engine valkey --major-engine-version 9 --connection-type public --user-group-id default.iam-user-group --tags Key=Project,Value=hecate --profile $Profile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'Valkey creation failed.' }
}
& $aws elasticache describe-serverless-caches --serverless-cache-name hecate-prod-vectors --profile $Profile --region $Region --query 'ServerlessCaches[0].{status:Status,endpoint:Endpoint,arn:ARN}'
