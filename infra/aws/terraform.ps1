[CmdletBinding()]
param(
    [string]$Profile = 'hecate',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$TerraformArgs
)

$ErrorActionPreference = 'Stop'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$terraform = (Get-Command terraform -ErrorAction SilentlyContinue).Source
if (-not $terraform) {
    $terraform = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Hashicorp.Terraform_Microsoft.Winget.Source_8wekyb3d8bbwe\terraform.exe'
}
if (-not (Test-Path -LiteralPath $terraform)) { throw 'Terraform is not installed.' }
$credentials = & $aws configure export-credentials --profile $Profile --format process | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'AWS login is required.' }
$values = @{
    AWS_ACCESS_KEY_ID = $credentials.AccessKeyId
    AWS_SECRET_ACCESS_KEY = $credentials.SecretAccessKey
    AWS_SESSION_TOKEN = $credentials.SessionToken
}
$previous = @{}
foreach ($name in $values.Keys) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $values[$name], 'Process')
}
Push-Location $PSScriptRoot
try {
    & $terraform @TerraformArgs
    if ($LASTEXITCODE -ne 0) { throw "Terraform failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
    foreach ($name in $values.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
