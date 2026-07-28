param(
  [string]$Root = "",
  [string]$ReportPath = "",
  [string]$PythonCommand = "",
  [string]$GitCommand = "",
  [switch]$Online
)

$ErrorActionPreference = "Continue"
$Results = @()
$ProviderValuesToRedact = @()
$ProviderEnvPrefixes = @("KB_LLM_", "KB_EMBEDDING_")
$ProviderEnvNames = @(
  "KB_LLM_BASE_URL",
  "KB_LLM_MODEL",
  "KB_LLM_API_KEY",
  "KB_EMBEDDING_BASE_URL",
  "KB_EMBEDDING_MODEL",
  "KB_EMBEDDING_API_KEY"
)
$OfflineProviderEnv = @{
  "KB_LLM_BASE_URL" = "http://127.0.0.1:9/kb-acceptance-offline-llm"
  "KB_LLM_MODEL" = "kb-acceptance-offline-llm"
  "KB_LLM_API_KEY" = "kb-acceptance-offline-key"
  "KB_EMBEDDING_BASE_URL" = ""
  "KB_EMBEDDING_MODEL" = ""
  "KB_EMBEDDING_API_KEY" = ""
}

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

function Test-ProviderEnvironmentName([string]$Name) {
  foreach ($prefix in $ProviderEnvPrefixes) {
    if ($Name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

foreach ($providerEnv in Get-ChildItem Env:) {
  if (Test-ProviderEnvironmentName $providerEnv.Name) {
    if ($providerEnv.Value) { $ProviderValuesToRedact += $providerEnv.Value }
  }
}

$ScriptPath = $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptPath)

function Write-FailClosed([string]$Classification, [string]$Message) {
  Write-Error "$Classification`: $Message"
  exit 1
}

function Test-ReportPathUnsafe([string]$Candidate) {
  if ([string]::IsNullOrWhiteSpace($Candidate)) { return $true }
  if ($Candidate.Contains("`0")) { return $true }
  if ($Candidate.Contains("*") -or $Candidate.Contains("?")) { return $true }
  foreach ($part in ($Candidate -split "[\\/]+")) {
    if ($part -eq "..") { return $true }
  }
  return $false
}

function Set-OfflineProviderEnvironment {
  foreach ($providerEnv in @(Get-ChildItem Env: | Where-Object { Test-ProviderEnvironmentName $_.Name })) {
    Remove-Item -LiteralPath "Env:$($providerEnv.Name)" -ErrorAction SilentlyContinue
  }
  foreach ($name in $ProviderEnvNames) {
    Set-Item -LiteralPath "Env:$name" -Value $OfflineProviderEnv[$name]
  }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
  $parent = Split-Path -Parent $Path
  if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  $utf8NoBom = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Write-MinimalSmokeSource([string]$sourcePath) {
  $content = "# Productization Smoke Source`n`nProductization smoke evidence quote anchors retrieval benchmark facts.`n"
  Write-Utf8NoBom $sourcePath $content
}

function Write-RetrievalBenchmark([string]$benchmarkPath, [string]$sourceId) {
  $record = [ordered]@{
    query = "productization smoke evidence"
    expected_source_ids = @($sourceId)
    privacy = "public"
    notes = "synthetic offline acceptance"
  }
  $json = $record | ConvertTo-Json -Compress
  Write-Utf8NoBom $benchmarkPath ($json + "`n")
}

function Redact-Text([string]$Text) {
  $redacted = [string]$Text
  foreach ($value in $ProviderValuesToRedact) {
    if ($value) {
      $redacted = $redacted.Replace($value, "[redacted]")
    }
  }
  foreach ($providerEnv in Get-ChildItem Env:) {
    if (Test-ProviderEnvironmentName $providerEnv.Name) {
      if ($providerEnv.Value) {
        $redacted = $redacted.Replace($providerEnv.Value, "[redacted]")
      }
    }
  }
  $redacted = [regex]::Replace($redacted, "s" + "k-[A-Za-z0-9_-]{8,}", "s" + "k-[redacted]")
  $redacted = [regex]::Replace($redacted, "(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._~+/=-]+", "Authorization: Bearer [redacted]")
  $redacted = [regex]::Replace($redacted, "(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [redacted]")
  $fieldNames = "api[_-]?key|password|token|prompt|provider[_-]?response|source[_-]?text|private[_-]?source[_-]?text"
  $quotedPattern = "(?i)([`"']?(?:$fieldNames)[`"']?\s*[:=]\s*[`"'])([^`"'\r\n]*)([`"'])"
  $redacted = [regex]::Replace($redacted, $quotedPattern, '$1[redacted]$3')
  $unquotedPattern = "(?i)\b($fieldNames)\s*[:=]\s*[^\r\n]*"
  $redacted = [regex]::Replace($redacted, $unquotedPattern, '$1=[redacted]')
  return $redacted
}

function Summarize-Text([string]$Text) {
  $redacted = Redact-Text $Text
  $redacted = $redacted -replace "`r`n", "`n"
  $redacted = $redacted -replace "`r", "`n"
  if ($redacted.Length -gt 500) {
    return $redacted.Substring(0, 500) + "[truncated]"
  }
  return $redacted
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

function Get-TreeSnapshot([string]$RootPath) {
  if (-not (Test-Path -LiteralPath $RootPath)) { return @() }
  $resolvedRoot = (Resolve-Path -LiteralPath $RootPath).Path
  $items = @()
  foreach ($path in Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force -File -ErrorAction SilentlyContinue) {
    $relative = $path.FullName.Substring($resolvedRoot.Length).TrimStart("\", "/")
    if ($relative -like ".git*") { continue }
    $hash = ""
    try {
      $hash = Get-Sha256Hex $path.FullName
    } catch {
      $hash = "unreadable"
    }
    $items += "$relative|$($path.Length)|$hash"
  }
  return ($items | Sort-Object)
}

function Classify-Failure([string]$Name, [string]$Output) {
  if (($Name -eq "git-status") -and $Output.Trim()) { return "dirty_worktree" }
  switch ($Name) {
    "unittest" { return "unittest_failed" }
    "setup-lint" { return "lint_failed" }
    "lint" { return "lint_failed" }
    "setup-status" { return "status_failed" }
    "status" { return "status_failed" }
    "setup-govern" { return "governance_failed" }
    "govern" { return "governance_failed" }
    "doctor" {
      if ($Output -match '(?i)"status"\s*:\s*"warning"|advisory|provider') {
        return "doctor_warning"
      }
      return "doctor_failed"
    }
    "lock-check" { return "lock_check_failed" }
    "backup" { return "backup_failed" }
    "restore" { return "restore_failed" }
    "migrate-check" { return "migrate_check_failed" }
    "setup-schema-check" { return "schema_check_failed" }
    "schema-check" { return "schema_check_failed" }
    "llm-preflight" { return "llm_preflight_failed" }
    "eval-search" { return "eval_search_failed" }
    "gateway-check" { return "gateway_check_failed" }
    "product-console" { return "product_console_failed" }
    "root-git-diff-check" { return "diff_check_failed" }
    "repository-git-diff-check" { return "diff_check_failed" }
    "docs-whitespace-qa" { return "docs_whitespace_failed" }
  }
  if ($Name -like "git-*") { return "git_failed" }
  if ($Name -like "setup-*") { return "setup_failed" }
  if ($Output -match "(?i)401|403|auth|unauthorized") { return "auth_failed" }
  if ($Output -match "(?i)timeout|connection|refused|network") { return "network_failed" }
  if ($Output -match "(?i)privacy|restricted|sensitive") { return "policy_blocked" }
  if ($Output -match "(?i)empty context|No source context") { return "empty_context" }
  return "command_failed"
}

function Test-NonFatalClassification([string]$Classification) {
  return $Classification -in @("doctor_warning")
}

function Invoke-AcceptanceStep(
  [string]$Name,
  [scriptblock]$Command,
  [bool]$CaptureNoWrite = $false
) {
  Write-Host "== $Name =="
  $before = $null
  if ($CaptureNoWrite) {
    $before = Get-TreeSnapshot $Root
  }

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

  if (($Name -eq "git-status") -and $stdout.Trim()) {
    $exitCode = 1
  }

  $combined = "$stdout`n$stderr"
  $classification = if ($exitCode -eq 0) { "pass" } else { Classify-Failure $Name $combined }
  $noWriteUnchanged = $null
  if ($CaptureNoWrite) {
    $after = Get-TreeSnapshot $Root
    $noWriteUnchanged = (($before -join "`n") -eq ($after -join "`n"))
  }

  $script:Results += [pscustomobject]@{
    name = $Name
    exit_code = $exitCode
    classification = $classification
    stdout_summary = Summarize-Text $stdout
    stderr_summary = Summarize-Text $stderr
    no_write_unchanged = $noWriteUnchanged
  }
}

function Invoke-DocsWhitespaceQa {
  $targets = @((Join-Path $RepoRoot "README.md"))
  $productDocs = Join-Path $RepoRoot "docs\product"
  if (Test-Path -LiteralPath $productDocs) {
    $targets += @(Get-ChildItem -LiteralPath $productDocs -Recurse -File -Filter "*.md" | ForEach-Object { $_.FullName })
  }
  $repoRootFullPath = [System.IO.Path]::GetFullPath($RepoRoot)
  $repoRootWithSeparator = $repoRootFullPath.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
  $issues = @()
  $repoStateMarkdown = @()
  $repoStateMarkdown += @(git -C $RepoRoot ls-files --others --exclude-standard -- "*.md" 2>$null)
  $repoStateMarkdown += @(git -C $RepoRoot diff --cached --name-only --diff-filter=ACM -- "*.md" 2>$null)
  foreach ($relativePath in $repoStateMarkdown) {
    if ([string]::IsNullOrWhiteSpace($relativePath)) { continue }
    try {
      $candidate = [System.IO.Path]::GetFullPath((Join-Path $repoRootFullPath $relativePath))
      if (
        $candidate.Equals($repoRootFullPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith($repoRootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)
      ) {
        $targets += $candidate
      }
    } catch {
      $issues += "$relativePath`: unsafe_repo_state_path"
    }
  }
  $dedupedTargets = New-Object System.Collections.Generic.List[string]
  $seenTargets = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($target in $targets) {
    if ($seenTargets.Add([System.IO.Path]::GetFullPath($target))) {
      $dedupedTargets.Add([System.IO.Path]::GetFullPath($target))
    }
  }
  $targets = $dedupedTargets

  $utf8Strict = New-Object System.Text.UTF8Encoding $false, $true
  foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target)) { continue }
    try {
      $bytes = [System.IO.File]::ReadAllBytes($target)
      $text = $utf8Strict.GetString($bytes)
    } catch {
      $issues += "$target`: utf8_decode_failed"
      continue
    }
    if ($bytes.Length -gt 0 -and $bytes[$bytes.Length - 1] -ne 10) {
      $issues += "$target`: missing_final_newline"
    }
    $lineNumber = 0
    foreach ($line in ($text -split "`n", -1)) {
      $lineNumber += 1
      $normalized = $line.TrimEnd("`r")
      if ($normalized -match "[ `t]+$") {
        $issues += "$target`:$lineNumber`: trailing_whitespace"
      }
    }
  }
  if ($issues.Count -gt 0) {
    throw ($issues -join "`n")
  }
  Write-Output "docs whitespace qa ok"
}

if (-not $Root) {
  $Root = Join-Path ([System.IO.Path]::GetTempPath()) ("kb-productization-acceptance-" + [guid]::NewGuid().ToString("N"))
}
$Root = [System.IO.Path]::GetFullPath($Root)

if (-not $ReportPath) {
  $reportDir = Join-Path ([System.IO.Path]::GetTempPath()) ("kb-productization-acceptance-report-" + [guid]::NewGuid().ToString("N"))
  $ReportPath = Join-Path $reportDir "productization-acceptance.jsonl"
}
if (Test-ReportPathUnsafe $ReportPath) {
  Write-FailClosed "unsafe_report_path" "ReportPath must not contain traversal, wildcards, or null bytes."
}
$ReportPath = [System.IO.Path]::GetFullPath($ReportPath)

if (-not $Online) {
  Set-OfflineProviderEnvironment
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null
$runOutputDir = Join-Path (Split-Path -Parent $Root) ((Split-Path -Leaf $Root) + "-acceptance-output")
New-Item -ItemType Directory -Force -Path $runOutputDir | Out-Null

$sourcePath = Join-Path $runOutputDir "productization-smoke-source.md"
$sourceHashSeed = "# Productization Smoke Source`n`nProductization smoke evidence quote anchors retrieval benchmark facts.`n"
$sourceHashBytes = [System.Text.Encoding]::UTF8.GetBytes($sourceHashSeed)
$sha = [System.Security.Cryptography.SHA256]::Create()
$sourceHash = [System.BitConverter]::ToString($sha.ComputeHash($sourceHashBytes)).Replace("-", "").ToLowerInvariant()
$sourceId = "src-" + $sourceHash.Substring(0, 12)
$benchmarkPath = Join-Path $Root "meta\evals\retrieval-benchmark.jsonl"

Invoke-AcceptanceStep "setup-init" { python -B -m kb init --root $Root }
Write-MinimalSmokeSource $sourcePath
Invoke-AcceptanceStep "setup-ingest" { python -B -m kb ingest $sourcePath --root $Root }
Invoke-AcceptanceStep "setup-rebuild-index" { python -B -m kb rebuild-index --root $Root }
Write-RetrievalBenchmark $benchmarkPath $sourceId
Invoke-AcceptanceStep "setup-schema-check" { python -B -m kb schema-check --root $Root --json }
Invoke-AcceptanceStep "setup-lint" { python -B -m kb lint --root $Root }
Invoke-AcceptanceStep "setup-status" { python -B -m kb status --root $Root }
Invoke-AcceptanceStep "setup-govern" { python -B -m kb govern --root $Root }
Invoke-AcceptanceStep "git-init" { git -C $Root init }
Invoke-AcceptanceStep "git-add-baseline" { git -C $Root add . }
Invoke-AcceptanceStep "git-commit-baseline" { git -C $Root -c user.name=ProductizationAcceptance -c user.email=productization-acceptance@example.invalid commit -m "productization acceptance baseline" }

$backupPath = Join-Path $runOutputDir "acceptance-backup.zip"
$restoreRoot = Join-Path (Split-Path -Parent $Root) ((Split-Path -Leaf $Root) + "-restore")
if (Test-Path -LiteralPath $restoreRoot) {
  Remove-Item -LiteralPath $restoreRoot -Recurse -Force
}

Invoke-AcceptanceStep "unittest" { python -B -m unittest discover -s tests -v }
Invoke-AcceptanceStep "lint" { python -B -m kb lint --root $Root }
Invoke-AcceptanceStep "status" { python -B -m kb status --root $Root }
Invoke-AcceptanceStep "doctor" { python -B -m kb doctor --root $Root --json } $true
Invoke-AcceptanceStep "lock-check" { python -B -m kb lock-check --root $Root --json } $true
Invoke-AcceptanceStep "backup" { python -B -m kb backup --root $Root --output $backupPath }
Invoke-AcceptanceStep "restore" { python -B -m kb restore --backup $backupPath --root $restoreRoot }
Invoke-AcceptanceStep "migrate-check" { python -B -m kb migrate-check --source $Root --restored $restoreRoot --json } $true
if ($Online) {
  Invoke-AcceptanceStep "llm-preflight" { python -B -m kb llm-preflight --root $Root --query "productization smoke" --title "Productization Smoke" --json } $true
} else {
  Invoke-AcceptanceStep "llm-preflight" { python -B -m kb llm-preflight --root $Root --query "productization smoke" --title "Productization Smoke" --offline --json } $true
}
Invoke-AcceptanceStep "eval-search" { python -B -m kb eval-search --root $Root --benchmark "meta/evals/retrieval-benchmark.jsonl" --json } $true
Invoke-AcceptanceStep "gateway-check" { python -B -m kb gateway-check --root $Root --json } $true
Invoke-AcceptanceStep "product-console" { python -B -m kb product-console --root $Root --json } $true
Invoke-AcceptanceStep "repository-git-diff-check" {
  Push-Location $RepoRoot
  try {
    git diff --check
  } finally {
    Pop-Location
  }
}
Invoke-AcceptanceStep "docs-whitespace-qa" { Invoke-DocsWhitespaceQa }
Invoke-AcceptanceStep "root-git-diff-check" { git -C $Root diff --check } $true
Invoke-AcceptanceStep "git-status" { git -C $Root status --short }

$reportParent = Split-Path -Parent $ReportPath
New-Item -ItemType Directory -Force -Path $reportParent | Out-Null
$reportLines = @($Results | ForEach-Object { $_ | ConvertTo-Json -Compress })
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($ReportPath, [string[]]$reportLines, $utf8NoBom)

Write-Host "Report: $ReportPath"
$fatalFailures = @($Results | Where-Object {
  ($_.exit_code -ne 0) -and -not (Test-NonFatalClassification $_.classification)
})
if ($fatalFailures.Count -gt 0) { exit 1 }
exit 0
