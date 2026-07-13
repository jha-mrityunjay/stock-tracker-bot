# Deploys the bot to AWS Lambda + Function URL, then points the Telegram webhook at it.
# Idempotent: run it again after any code change to redeploy.
#
#   1. Copy .env.example to .env and fill in your secrets
#   2. .\deploy.ps1

$ErrorActionPreference = "Stop"

$FunctionName = "stock-tracker-bot"
$RoleName     = "stock-tracker-bot-role"
$Region       = "ap-south-1"   # Mumbai — closest to Upstox and to you
$Runtime      = "python3.13"

# --- Load .env ---------------------------------------------------------
if (-not (Test-Path ".env")) { throw "No .env file. Copy .env.example to .env and fill it in." }
$cfg = @{}
foreach ($line in Get-Content ".env") {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $k, $v = $line -split '=', 2
    $cfg[$k.Trim()] = $v.Trim().Trim('"')
}
foreach ($k in @("TELEGRAM_TOKEN", "SUPABASE_URL", "SUPABASE_KEY", "UPSTOX_TOKEN", "WEBHOOK_SECRET")) {
    if (-not $cfg[$k]) { throw "Missing $k in .env" }
}

$AccountId = (aws sts get-caller-identity --query Account --output text)
Write-Host "AWS account $AccountId, region $Region" -ForegroundColor Cyan

# --- IAM role ----------------------------------------------------------
$roleArn = $null
try { $roleArn = (aws iam get-role --role-name $RoleName --query "Role.Arn" --output text 2>$null) } catch {}

if (-not $roleArn) {
    Write-Host "Creating IAM role $RoleName..." -ForegroundColor Yellow
    $trust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    $trust | Out-File -FilePath "trust-policy.json" -Encoding ascii
    $roleArn = (aws iam create-role --role-name $RoleName `
        --assume-role-policy-document file://trust-policy.json `
        --query "Role.Arn" --output text)
    aws iam attach-role-policy --role-name $RoleName `
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    Remove-Item "trust-policy.json"
    Write-Host "Waiting for IAM role to propagate..." -ForegroundColor Yellow
    Start-Sleep -Seconds 12
}

# --- Package -----------------------------------------------------------
if (Test-Path "function.zip") { Remove-Item "function.zip" }
Compress-Archive -Path "handler.py", "nse_equity.json" -DestinationPath "function.zip"
Write-Host "Packaged function.zip ($([math]::Round((Get-Item function.zip).Length / 1KB)) KB)" -ForegroundColor Cyan

# --- Environment variables (as JSON, to survive special characters) -----
$envJson = @{ Variables = @{
    TELEGRAM_TOKEN = $cfg["TELEGRAM_TOKEN"]
    SUPABASE_URL   = $cfg["SUPABASE_URL"]
    SUPABASE_KEY   = $cfg["SUPABASE_KEY"]
    UPSTOX_TOKEN   = $cfg["UPSTOX_TOKEN"]
    WEBHOOK_SECRET = $cfg["WEBHOOK_SECRET"]
} } | ConvertTo-Json -Compress
$envJson | Out-File -FilePath "env.json" -Encoding ascii

# --- Create or update the function --------------------------------------
$exists = $true
try { aws lambda get-function --function-name $FunctionName --region $Region 2>$null | Out-Null } catch { $exists = $false }
if (-not $?) { $exists = $false }

if ($exists) {
    Write-Host "Updating existing function..." -ForegroundColor Yellow
    aws lambda update-function-code --function-name $FunctionName `
        --zip-file fileb://function.zip --region $Region | Out-Null
    aws lambda wait function-updated --function-name $FunctionName --region $Region
    aws lambda update-function-configuration --function-name $FunctionName `
        --environment file://env.json --timeout 30 --memory-size 256 `
        --region $Region | Out-Null
} else {
    Write-Host "Creating function..." -ForegroundColor Yellow
    aws lambda create-function --function-name $FunctionName `
        --runtime $Runtime --handler handler.lambda_handler --role $roleArn `
        --zip-file fileb://function.zip --timeout 30 --memory-size 256 `
        --environment file://env.json --region $Region | Out-Null
}
aws lambda wait function-updated --function-name $FunctionName --region $Region
Remove-Item "env.json"

# --- Log retention ------------------------------------------------------
# Logs default to "never expire". CloudWatch's always-free tier is 5 GB/month and
# this bot won't come close, but capping retention guarantees it can never creep
# into a bill. Create the group first so this works on a brand-new function.
try { aws logs create-log-group --log-group-name "/aws/lambda/$FunctionName" --region $Region 2>$null | Out-Null } catch {}
aws logs put-retention-policy --log-group-name "/aws/lambda/$FunctionName" `
    --retention-in-days 14 --region $Region | Out-Null

# --- Function URL -------------------------------------------------------
$url = $null
try { $url = (aws lambda get-function-url-config --function-name $FunctionName --region $Region --query FunctionUrl --output text 2>$null) } catch {}

if (-not $url) {
    Write-Host "Creating public Function URL..." -ForegroundColor Yellow
    $url = (aws lambda create-function-url-config --function-name $FunctionName `
        --auth-type NONE --region $Region --query FunctionUrl --output text)
}

# Since October 2025 a public function URL needs BOTH lambda:InvokeFunctionUrl and
# lambda:InvokeFunction, added as separate statements. With only the first, every
# request 403s with an AWS-level "Forbidden" before it ever reaches handler.py.
# InvokedViaFunctionUrl also stops the function being called via the raw Invoke API.
# Both are no-ops if already present, so this stays idempotent.
try {
    aws lambda add-permission --function-name $FunctionName `
        --statement-id FunctionURLAllowPublicAccess --action lambda:InvokeFunctionUrl `
        --principal "*" --function-url-auth-type NONE --region $Region 2>$null | Out-Null
} catch {}
try {
    aws lambda add-permission --function-name $FunctionName `
        --statement-id FunctionURLInvokeAllowPublicAccess --action lambda:InvokeFunction `
        --principal "*" --invoked-via-function-url --region $Region 2>$null | Out-Null
} catch {}
$url = $url.TrimEnd("/")
Write-Host "Function URL: $url" -ForegroundColor Green

# --- Point Telegram at it ------------------------------------------------
Write-Host "Registering Telegram webhook..." -ForegroundColor Yellow
$body = @{
    url = $url
    secret_token = $cfg["WEBHOOK_SECRET"]
    drop_pending_updates = $true
    allowed_updates = @("message", "callback_query")
} | ConvertTo-Json -Compress

$resp = Invoke-RestMethod -Method Post -ContentType "application/json" `
    -Uri "https://api.telegram.org/bot$($cfg['TELEGRAM_TOKEN'])/setWebhook" -Body $body

if ($resp.ok) {
    Write-Host "`nDeployed. Open Telegram and send /start to your bot." -ForegroundColor Green
} else {
    throw "Telegram setWebhook failed: $($resp | ConvertTo-Json -Compress)"
}
