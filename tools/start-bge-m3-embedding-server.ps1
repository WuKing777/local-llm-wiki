param(
    [string]$BaseDir = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ([string]::IsNullOrWhiteSpace($BaseDir)) {
    $BaseDir = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "LocalLlmWiki\BGE-M3"
}
$CacheDir = Join-Path $BaseDir "hf-cache"
$Port = 18080
$BaseUrl = "http://127.0.0.1:$Port/v1"

New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

[Environment]::SetEnvironmentVariable("KB_EMBEDDING_BASE_URL", $BaseUrl, "User")
[Environment]::SetEnvironmentVariable("KB_EMBEDDING_MODEL", "bge-m3", "User")
[Environment]::SetEnvironmentVariable("KB_EMBEDDING_TIMEOUT_SECONDS", "300", "User")
[Environment]::SetEnvironmentVariable("KB_EMBEDDING_API_KEY", $null, "User")
[Environment]::SetEnvironmentVariable("BGE_M3_CACHE_DIR", $CacheDir, "User")

$env:KB_EMBEDDING_BASE_URL = $BaseUrl
$env:KB_EMBEDDING_MODEL = "bge-m3"
$env:KB_EMBEDDING_TIMEOUT_SECONDS = "300"
$env:BGE_M3_CACHE_DIR = $CacheDir
Remove-Item Env:KB_EMBEDDING_API_KEY -ErrorAction SilentlyContinue

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    Write-Output "bge-m3 endpoint already listening at $BaseUrl pid=$($existing.OwningProcess)"
    exit 0
}

$python = (Get-Command python).Source
$stdout = Join-Path $BaseDir "server.out.log"
$stderr = Join-Path $BaseDir "server.err.log"
$process = Start-Process `
    -FilePath $python `
    -ArgumentList @("-B", "-m", "uvicorn", "local_bge_m3_server:app", "--app-dir", (Join-Path $RepoRoot "tools"), "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null
        Write-Output "bge-m3 endpoint listening at $BaseUrl pid=$($process.Id)"
        exit 0
    } catch {
        Start-Sleep -Seconds 1
    }
}

throw "bge-m3 endpoint did not become healthy; inspect $stderr"
