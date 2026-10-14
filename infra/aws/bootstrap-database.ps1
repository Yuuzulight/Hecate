[CmdletBinding()]
param(
    [string]$Profile = 'hecate',
    [string]$Region = 'ap-southeast-2',
    [string]$Image = '426714790799.dkr.ecr.ap-southeast-2.amazonaws.com/hecate-prod-batch:20261014-iam'
)
$ErrorActionPreference = 'Stop'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$result = & $aws rds describe-db-clusters --db-cluster-identifier hecate-prod --profile $Profile --region $Region 2>&1
if ($LASTEXITCODE -ne 0) {
    if (($result -join ' ') -notmatch 'DBClusterNotFoundFault') { throw ($result -join ' ') }
    & $aws rds create-db-cluster --db-cluster-identifier hecate-prod --engine aurora-postgresql --with-express-configuration --master-username dataflow --deletion-protection --serverless-v2-scaling-configuration 'MinCapacity=0,MaxCapacity=1,SecondsUntilAutoPause=900' --tags Key=Project,Value=hecate Key=Environment,Value=prod --profile $Profile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'Aurora Express creation failed.' }
}
& $aws rds wait db-cluster-available --db-cluster-identifier hecate-prod --profile $Profile --region $Region
if ($LASTEXITCODE -ne 0) { throw 'Aurora is not available.' }
$endpoint = & $aws rds describe-db-clusters --db-cluster-identifier hecate-prod --profile $Profile --region $Region --query 'DBClusters[0].Endpoint' --output text
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Aurora endpoint.' }
$previousPassword = $env:DB_PASSWORD
try {
    $env:DB_PASSWORD = & $aws rds generate-db-auth-token --hostname $endpoint --port 5432 --username dataflow --profile $Profile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'Cannot generate database token.' }
    docker run --rm --entrypoint python -e DB_PASSWORD -e "DB_HOST=$endpoint" -e DB_USER=dataflow -e DB_NAME=postgres -e PGSSLMODE=verify-full -e PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt $Image -c "import aws_batch; c=aws_batch.wait_for_database(); c.autocommit=True; cur=c.cursor(); cur.execute('SELECT 1 FROM pg_database WHERE datname=%s', ('hecate',)); exists=cur.fetchone(); cur.execute('CREATE DATABASE hecate') if not exists else None; c.close(); print('Hecate warehouse ready')"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize Hecate warehouse.' }
} finally {
    $env:DB_PASSWORD = $previousPassword
}
