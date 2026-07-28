param(
  [string]$Root = "",
  [string]$ReportPath = "",
  [string]$PythonCommand = "",
  [string]$GitCommand = ""
)

$ErrorActionPreference = "Continue"
$Results = @()

function Resolve-CommandOverride([string]$Candidate, [string]$Label) {
  if ([string]::IsNullOrWhiteSpace($Candidate)) { return "" }
  try {
    $resolved = (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
  } catch {
    throw "$Label command override does not exist."
  }
  if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
    throw "$Label command override must be a file."
  }
  return $resolved
}

function ConvertTo-ProcessArgument([AllowEmptyString()][string]$Value) {
  if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
  if ($Value -notmatch '[\s"]') { return $Value }
  $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
  $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
  return '"' + $escaped + '"'
}

function Invoke-CommandOverrideProcess([string]$CommandPath, [object[]]$Arguments) {
  $extension = [System.IO.Path]::GetExtension($CommandPath)
  if ($extension.Equals(".ps1", [System.StringComparison]::OrdinalIgnoreCase)) {
    $hostPath = (Get-Process -Id $PID).Path
    $fileName = $hostPath
    $processArguments = @(
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      $CommandPath
    ) + @($Arguments)
  } elseif (
    $extension.Equals(".cmd", [System.StringComparison]::OrdinalIgnoreCase) -or
    $extension.Equals(".bat", [System.StringComparison]::OrdinalIgnoreCase)
  ) {
    $fileName = $env:ComSpec
    $processArguments = @("/d", "/s", "/c", $CommandPath) + @($Arguments)
  } else {
    $fileName = $CommandPath
    $processArguments = @($Arguments)
  }

  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = $fileName
  $startInfo.Arguments = (@(
    $processArguments | ForEach-Object { ConvertTo-ProcessArgument ([string]$_) }
  ) -join " ")
  $startInfo.WorkingDirectory = (Get-Location).Path
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true

  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) { throw "Command override process did not start." }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    $exitCode = $process.ExitCode
  } finally {
    $process.Dispose()
  }

  return [pscustomobject]@{
    stdout = [string]$stdout
    stderr = [string]$stderr
    exit_code = [int]$exitCode
  }
}

function Invoke-ResolvedCommandOverride([string]$CommandPath, [object[]]$Arguments) {
  $result = Invoke-CommandOverrideProcess $CommandPath $Arguments
  if ($result.stdout) {
    Write-Output $result.stdout.TrimEnd("`r", "`n")
  }
  if ($result.stderr) {
    Write-Error -Message $result.stderr.TrimEnd("`r", "`n") -ErrorAction Continue
  }
  $global:LASTEXITCODE = $result.exit_code
}

$ResolvedPythonCommand = Resolve-CommandOverride $PythonCommand "Python"
$ResolvedGitCommand = Resolve-CommandOverride $GitCommand "Git"
if ($ResolvedPythonCommand) {
  $script:ResolvedPythonCommand = $ResolvedPythonCommand
  Set-Item -LiteralPath function:python -Value {
    Invoke-ResolvedCommandOverride $script:ResolvedPythonCommand $args
  }
}
if ($ResolvedGitCommand) {
  $script:ResolvedGitCommand = $ResolvedGitCommand
  Set-Item -LiteralPath function:git -Value {
    Invoke-ResolvedCommandOverride $script:ResolvedGitCommand $args
  }
}

if ([string]::IsNullOrWhiteSpace($Root)) {
  Write-Error "Root is required. Pass -Root with an approved local knowledge-base root. This script has no default vault path."
  exit 2
}

if (-not $ReportPath) {
  $ReportPath = Join-Path $Root "docs\reviews\acceptance-run.jsonl"
}

function Get-Sha256Hex([string]$Path) {
  $stream = [System.IO.File]::OpenRead($Path)
  try {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
      return [System.BitConverter]::ToString(
        $sha256.ComputeHash($stream)
      ).Replace("-", "")
    } finally {
      $sha256.Dispose()
    }
  } finally {
    $stream.Dispose()
  }
}

function Get-NoWriteSnapshot([string]$RootPath) {
  $draftCount = 0
  $logHash = ""
  $reviewHash = ""
  $eventCount = 0
  $draftDir = Join-Path $RootPath "wiki\_drafts"
  $logPath = Join-Path $RootPath "meta\log.md"
  $reviewPath = Join-Path $RootPath "meta\review-queue.md"
  $dbPath = Join-Path $RootPath "db\kb.sqlite3"

  if (Test-Path -LiteralPath $draftDir) {
    $draftCount = @(Get-ChildItem -LiteralPath $draftDir -File -ErrorAction SilentlyContinue).Count
  }
  if (Test-Path -LiteralPath $logPath) {
    $logHash = Get-Sha256Hex $logPath
  }
  if (Test-Path -LiteralPath $reviewPath) {
    $reviewHash = Get-Sha256Hex $reviewPath
  }
  if (Test-Path -LiteralPath $dbPath) {
    try {
      $eventCountText = python -c "import sqlite3,sys; db=sys.argv[1]; con=sqlite3.connect(db); exists=con.execute(""select count(*) from sqlite_master where type='table' and name='events'"").fetchone()[0]; print(con.execute('select count(*) from events').fetchone()[0] if exists else 0); con.close()" $dbPath
      if ($LASTEXITCODE -eq 0 -and $eventCountText) {
        $eventCount = [int]$eventCountText
      }
    } catch {
      $eventCount = -1
    }
  }

  [pscustomobject]@{
    draft_count = $draftCount
    log_hash = $logHash
    review_hash = $reviewHash
    event_count = $eventCount
  }
}

function Redact-Text([string]$Text) {
  $redacted = [string]$Text
  foreach ($name in @("KB_LLM_API_KEY", "KB_EMBEDDING_API_KEY")) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value) {
      $redacted = $redacted.Replace($value, "[redacted]")
    }
  }
  $apiKeyPattern = "s" + "k-[A-Za-z0-9_-]{8,}"
  $apiKeyReplacement = "s" + "k-[redacted]"
  $redacted = [regex]::Replace($redacted, $apiKeyPattern, $apiKeyReplacement)
  $redacted = [regex]::Replace($redacted, "(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._~+/=-]+", "Authorization: Bearer [redacted]")
  $redacted = [regex]::Replace($redacted, "(?i)(api[_-]?key|password|token)\s*[:=]\s*[^ \r\n]+", '$1=[redacted]')
  return $redacted
}

function Summarize-Text([string]$Text) {
  $redacted = Redact-Text $Text
  $redacted = $redacted -replace "\r\n", "`n"
  if ($redacted.Length -gt 500) {
    return $redacted.Substring(0, 500) + "[truncated]"
  }
  return $redacted
}

function Classify-Failure([string]$Name, [string]$Output) {
  $trimmed = $Output.Trim()
  if (($Name -eq "git-status") -and $trimmed) { return "dirty_worktree" }
  if ($Name -match "ocr") { return "ocr_failed" }
  if ($Name -match "embedding|vector|semantic|hybrid") { return "embedding_failed" }
  if ($Name -eq "llm-check") {
    if ($Output -match "KB_LLM_BASE_URL|KB_LLM_MODEL|KB_LLM_API_KEY|required") { return "missing_config" }
    if ($Output -match "(?i)401|403|auth|unauthorized") { return "auth_failed" }
    return "deepseek_failed"
  }
  if ($Name -match "llm-draft|validate-draft|publish-draft") {
    if ($Output -match "(?i)empty context|No source context") { return "empty_context" }
    if ($Output -match "KB_LLM_BASE_URL|KB_LLM_MODEL|KB_LLM_API_KEY|required") { return "missing_config" }
    if ($Output -match "(?i)privacy|restricted|sensitive") { return "policy_blocked" }
    return "llm_dry_run_failed"
  }
  if ($Output -match "KB_LLM_BASE_URL|KB_LLM_MODEL|KB_LLM_API_KEY|required") { return "missing_config" }
  if ($Output -match "(?i)401|403|auth|unauthorized") { return "auth_failed" }
  if ($Output -match "(?i)timeout|connection|refused|network") { return "network_failed" }
  if ($Output -match "(?i)model|response format|invalid response") { return "model_failed" }
  if ($Output -match "(?i)empty context|No source context") { return "empty_context" }
  if ($Output -match "(?i)privacy|restricted|sensitive") { return "policy_blocked" }
  return "command_failed"
}

function Invoke-KbStep([string]$Name, [scriptblock]$Command, [bool]$ContinueOnFailure = $false, [bool]$CaptureNoWrite = $false) {
  Write-Host "== $Name =="
  $before = $null
  if ($CaptureNoWrite) { $before = Get-NoWriteSnapshot $Root }

  $stdoutItems = New-Object System.Collections.Generic.List[string]
  $stderrItems = New-Object System.Collections.Generic.List[string]
  $capturedOutput = @()
  $exitCode = 0
  try {
    $global:LASTEXITCODE = 0
    $capturedOutput = @(& $Command *>&1)
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 0 }
    foreach ($item in $capturedOutput) {
      if ($item -is [System.Management.Automation.ErrorRecord]) {
        [void]$stderrItems.Add($item.ToString())
      } else {
        [void]$stdoutItems.Add([string]$item)
      }
    }
  } catch {
    $exitCode = 1
    [void]$stderrItems.Add($_.ToString())
  }

  $stdout = $stdoutItems -join [Environment]::NewLine
  $stderr = $stderrItems -join [Environment]::NewLine
  $combined = "$stdout`n$stderr"
  if (($Name -eq "git-status") -and $stdout.Trim()) {
    $exitCode = 1
  }
  $classification = if ($exitCode -eq 0) { "pass" } else { Classify-Failure $Name $combined }

  $noWriteUnchanged = $null
  if ($CaptureNoWrite) {
    $after = Get-NoWriteSnapshot $Root
    $noWriteUnchanged = (($before | ConvertTo-Json -Compress) -eq ($after | ConvertTo-Json -Compress))
  }

  $script:Results += [pscustomobject]@{
    name = $Name
    exit_code = $exitCode
    classification = $classification
    no_write_unchanged = $noWriteUnchanged
    stdout_summary = Summarize-Text $stdout
    stderr_summary = Summarize-Text $stderr
  }
}

Invoke-KbStep "lint" { python -B -m kb lint --root $Root }
Invoke-KbStep "status" { python -B -m kb status --root $Root }
Invoke-KbStep "govern" { python -B -m kb govern --root $Root }
Invoke-KbStep "ocr-check" { python -B -m kb ocr-check } $true

$tmpRoot = Join-Path $env:TEMP ("kb-ocr-smoke-" + [guid]::NewGuid().ToString("N"))
try {
  Invoke-KbStep "ocr-temp-init" { python -B -m kb init --root $tmpRoot } $true
  $tmpImage = Join-Path $tmpRoot "ocr-smoke-chi-eng.png"
  Invoke-KbStep "ocr-fixture" { python -B -m kb ocr-fixture --output $tmpImage --text "外脑 OCR smoke 123" } $true
  Invoke-KbStep "ingest-ocr-temp-root" { python -B -m kb ingest-ocr --root $tmpRoot --lang chi_sim+eng $tmpImage } $true
  Invoke-KbStep "ocr-temp-lint" { python -B -m kb lint --root $tmpRoot } $true
  Invoke-KbStep "ocr-temp-status" { python -B -m kb status --root $tmpRoot } $true
} finally {
  if (Test-Path -LiteralPath $tmpRoot) {
    $tmpRootResolved = (Resolve-Path -LiteralPath $tmpRoot).Path
    $tmpParentResolved = (Resolve-Path -LiteralPath $env:TEMP).Path
    if (-not $tmpRootResolved.StartsWith($tmpParentResolved, [StringComparison]::OrdinalIgnoreCase)) { throw "unsafe temp cleanup path" }
    Remove-Item -LiteralPath $tmpRootResolved -Recurse -Force
  }
}

Invoke-KbStep "embedding-check" { python -B -m kb embedding-check } $true
Invoke-KbStep "vector-rebuild" { python -B -m kb vector-rebuild --root $Root } $true
Invoke-KbStep "semantic-search" { python -B -m kb semantic-search "我是谁" --root $Root } $true
Invoke-KbStep "hybrid-search" { python -B -m kb hybrid-search "我是谁" --root $Root } $true
Invoke-KbStep "llm-check" { python -B -m kb llm-check } $true $true
Invoke-KbStep "self-statement-dry-run-source" { python -B -m kb self-statement --root $Root --text "我希望把这个知识库作为外脑使用。" --event-date "2026-07-01" --privacy personal --confidence confirmed --input-method chat }
$draft = ""
Invoke-KbStep "llm-draft" { $script:draft = python -B -m kb llm-draft --root $Root --query "我是谁" --title "LLM 发布闭环测试" } $true $true
$draftPath = @($draft | Where-Object { $_ }) | Select-Object -First 1
if ($draftPath) {
  Invoke-KbStep "validate-draft" { python -B -m kb validate-draft --root $Root $draftPath --target "LLM 发布闭环测试" } $true
  Invoke-KbStep "publish-draft" { python -B -m kb publish-draft --root $Root $draftPath --target "LLM 发布闭环测试" } $true
}
Invoke-KbStep "post-lint" { python -B -m kb lint --root $Root }
Invoke-KbStep "post-status" { python -B -m kb status --root $Root }
Invoke-KbStep "post-govern" { python -B -m kb govern --root $Root }
Invoke-KbStep "git-status" { git -C $Root status --short }
Invoke-KbStep "git-diff-check" { git -C $Root diff --check }

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReportPath) | Out-Null
$reportLines = @($Results | ForEach-Object { $_ | ConvertTo-Json -Compress })
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($ReportPath, [string[]]$reportLines, $utf8NoBom)
if ($Results | Where-Object { $_.exit_code -ne 0 }) { exit 1 }
exit 0
