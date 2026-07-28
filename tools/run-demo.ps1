param(
    [string]$DemoRoot = "examples\demo-root",
    [string]$ReportPath = "",
    [switch]$AllowCustomRoot,
    [string]$Python = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-FromRepo {
    param([string]$PathText)

    if ([System.IO.Path]::IsPathRooted($PathText)) {
        return $PathText
    }
    return Join-Path $script:RepoRoot $PathText
}

function Get-RelativePathCompat {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseFull = [System.IO.Path]::GetFullPath($BasePath)
    if (-not $baseFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $baseFull += [System.IO.Path]::DirectorySeparatorChar
    }
    $targetFull = [System.IO.Path]::GetFullPath($TargetPath)
    $baseUri = [System.Uri]::new($baseFull)
    $targetUri = [System.Uri]::new($targetFull)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace("/", "\")
}

function Get-Sha256Hex {
    param([string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return [System.BitConverter]::ToString(
                $sha256.ComputeHash($stream)
            ).Replace("-", "")
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function ConvertTo-RepoDisplayPath {
    param([string]$ResolvedPath)

    try {
        return (Get-RelativePathCompat -BasePath $script:RepoRoot -TargetPath $ResolvedPath).Replace("\", "/")
    }
    catch {
        return "<outside-repo>"
    }
}

function ConvertTo-DisplayRoot {
    param([bool]$UseCustomRoot)

    if ($UseCustomRoot) {
        return "<custom-root>"
    }
    return "<temp-synthetic-root>"
}

function Quote-ProcessArgument {
    param([string]$Value)

    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Join-ProcessArguments {
    param([string[]]$Arguments)

    return ($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join " "
}

function Redact-DemoText {
    param([string]$Text)

    if ([string]::IsNullOrEmpty($Text)) {
        return ""
    }

    $redacted = $Text
    foreach ($path in @($script:RepoRoot, $script:IsolatedBase)) {
        if ([string]::IsNullOrWhiteSpace($path)) {
            continue
        }
        $redacted = $redacted.Replace($path, "<path>")
        $redacted = $redacted.Replace($path.Replace("\", "\\"), "<path>")
    }
    $redacted = $redacted -replace '(?i)[A-Z]:(?:\\\\|\\|/)[^\s,}"]+', '<path>'
    $redacted = $redacted -replace '(?i)\bLOCALAPPDATA\b', '<local-config-dir>'
    $redacted = $redacted -replace '(?i)\bAPPDATA\b', '<config-dir>'
    $redacted = $redacted -replace '(?i)\bUsers\b', '<user-dir>'
    $redacted = $redacted -replace '(?i)\bAdministrator\b', '<account>'
    $redacted = $redacted -replace '(?i)https?://[^\s,}"]+', '<url>'
    $redacted = $redacted -replace '(?i)\bprompt\s*[:=]\s*["'']?[^,}`r`n]+', 'prompt=<redacted>'
    $redacted = $redacted -replace '(?i)\b(?:full[_ -]?)?provider[_ -]?response\s*[:=]\s*["'']?[^,}`r`n]+', 'provider_response=<redacted>'
    $redacted = $redacted -replace '(?i)\bprivate[_ -]?source[_ -]?text\s*[:=]\s*["'']?[^,}`r`n]+', 'private_source_text=<redacted>'
    $redacted = $redacted -replace '(?i)\bsource[_ -]?chunk\s*[:=]\s*["'']?[^,}`r`n]+', 'source_chunk=<redacted>'
    $redacted = $redacted -replace '(?i)sk-[A-Za-z0-9][A-Za-z0-9_-]{8,}', '<secret>'
    $redacted = $redacted -replace '(?i)gh[pousr]_[A-Za-z0-9_]{16,}', '<secret>'
    $redacted = $redacted -replace 'AKIA[0-9A-Z]{16}', '<secret>'
    $redacted = $redacted -replace '(?i)xox[baprs]-[A-Za-z0-9-]{10,}', '<secret>'
    $redacted = $redacted -replace '(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}', 'bearer <secret>'
    $redacted = $redacted -replace '(?i)api[_-]?key\s*=\s*[''"]?[A-Za-z0-9._~+/-]{12,}', 'api_key=<secret>'
    return $redacted
}

function Summarize-DemoText {
    param([string]$Text)

    $safe = Redact-DemoText $Text
    $lines = $safe -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 } | Select-Object -First 4
    $summary = ($lines -join " | ").Trim()
    if ($summary.Length -gt 700) {
        return $summary.Substring(0, 700) + "..."
    }
    return $summary
}

function New-DemoProcessInfo {
    param(
        [string]$FileName,
        [string[]]$Arguments,
        [string]$AppDataRoot,
        [string]$LocalAppDataRoot
    )

    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $FileName
    $info.Arguments = Join-ProcessArguments $Arguments
    $info.WorkingDirectory = $script:RepoRoot
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.EnvironmentVariables["APPDATA"] = $AppDataRoot
    $info.EnvironmentVariables["LOCALAPPDATA"] = $LocalAppDataRoot
    foreach ($name in @(
        "KB_LLM_API_KEY",
        "KB_LLM_BASE_URL",
        "KB_LLM_MODEL",
        "KB_LLM_TIMEOUT_SECONDS",
        "KB_LLM_RESPONSE_FORMAT",
        "KB_LLM_MAX_TOKENS",
        "KB_LLM_THINKING",
        "KB_LLM_REASONING_EFFORT",
        "KB_EMBEDDING_API_KEY",
        "KB_EMBEDDING_BASE_URL",
        "KB_EMBEDDING_MODEL",
        "KB_EMBEDDING_TIMEOUT_SECONDS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "HF_TOKEN"
    )) {
        $info.EnvironmentVariables[$name] = ""
    }
    return $info
}

function Invoke-DemoCommand {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [string]$AppDataRoot,
        [string]$LocalAppDataRoot
    )

    $info = New-DemoProcessInfo -FileName $Python -Arguments $Arguments -AppDataRoot $AppDataRoot -LocalAppDataRoot $LocalAppDataRoot
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    $status = "completed"
    $classification = "exit_zero"
    if ($process.ExitCode -ne 0) {
        $status = "reported_issues"
        $classification = "nonzero_exit_recorded"
    }

    try {
        $json = $stdout | ConvertFrom-Json
        if ($json.status) {
            $status = [string]$json.status
        }
        if ($json.classification) {
            $classification = [string]$json.classification
        }
        elseif ($json.health -and $json.health.status) {
            $classification = "health_" + [string]$json.health.status
        }
    }
    catch {
    }

    return [ordered]@{
        name = $Name
        exit_code = $process.ExitCode
        status = $status
        classification = $classification
        stdout_summary = Summarize-DemoText $stdout
        stderr_summary = Summarize-DemoText $stderr
    }
}

function New-DemoStoryStep {
    param(
        [string]$Name,
        [string]$Classification,
        [string]$Summary
    )

    return [ordered]@{
        name = $Name
        exit_code = 0
        status = "completed"
        classification = $Classification
        stdout_summary = Summarize-DemoText $Summary
        stderr_summary = ""
    }
}

function Assert-DemoStepSucceeded {
    param([hashtable]$Step)

    if ([int]$Step.exit_code -ne 0) {
        throw "Demo step failed: $($Step.name) classification=$($Step.classification) stdout=$($Step.stdout_summary) stderr=$($Step.stderr_summary)"
    }
}

function Get-RegexValue {
    param(
        [string]$Text,
        [string]$Pattern,
        [string]$Label
    )

    $match = [regex]::Match($Text, $Pattern)
    if (-not $match.Success) {
        throw "Could not parse $Label from demo output."
    }
    return $match.Value
}

function Get-TreeFingerprint {
    param([string]$Root)

    $fingerprint = @{}
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $fingerprint
    }
    Get-ChildItem -LiteralPath $Root -Recurse -File -Force | ForEach-Object {
        $relative = (Get-RelativePathCompat -BasePath $Root -TargetPath $_.FullName).Replace("\", "/")
        $fingerprint[$relative] = Get-Sha256Hex -Path $_.FullName
    }
    return $fingerprint
}

function Test-TreeFingerprintEqual {
    param(
        [hashtable]$Before,
        [hashtable]$After
    )

    if ($Before.Count -ne $After.Count) {
        return $false
    }
    foreach ($key in $Before.Keys) {
        if (-not $After.ContainsKey($key)) {
            return $false
        }
        if ($Before[$key] -ne $After[$key]) {
            return $false
        }
    }
    return $true
}

function ConvertTo-JsonLine {
    param([object]$Value)

    return ConvertTo-Json -InputObject $Value -Compress -Depth 20
}

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Text
    )

    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Text, $utf8NoBom)
}

function Write-DeterministicDraft {
    param(
        [string]$Root,
        [string]$SourceId
    )

    $claimText = "The offline local workflow is safe for public release"
    $evidenceQuote = "$claimText."
    $draftRelative = "wiki/_drafts/synthetic-demo-story.md"
    $draftPath = Join-Path $Root $draftRelative
    New-Item -ItemType Directory -Path (Split-Path -Parent $draftPath) -Force | Out-Null

    $claim = [ordered]@{
        claim_id = "claim-1"
        paragraph = 1
        text = $claimText
        evidence = @(
            [ordered]@{
                chunk = "$SourceId#0"
                quote = $evidenceQuote
            }
        )
    }
    $metadata = [ordered]@{
        draft_id = "synthetic-demo-story"
        title = "Synthetic Demo Story"
        query = "offline local workflow"
        created_at = "2026-07-09T00:00:00Z"
        model = "deterministic-local-demo"
        prompt_hash = ("0" * 64)
        context_sources = @($SourceId)
        context_chunks = @("$SourceId#0")
        claims = @($claim)
    }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("---")
    foreach ($key in $metadata.Keys) {
        $lines.Add("${key}: " + (ConvertTo-JsonLine $metadata[$key]))
    }
    $lines.Add("---")
    $lines.Add("")
    $lines.Add("# [[Synthetic Demo Story]]")
    $lines.Add("")
    $lines.Add("$claimText $SourceId.")
    Write-Utf8NoBom -Path $draftPath -Text (($lines -join "`n") + "`n")

    return [ordered]@{
        draft_path = $draftRelative
        absolute_path = $draftPath
        target = "Synthetic Demo Story"
        published_path = "wiki/synthetic-demo-story.md"
        source_id = $SourceId
        evidence_quote = $evidenceQuote
    }
}

function Remove-DemoOnlyCandidateQueue {
    param(
        [string]$Root,
        [bool]$UseCustomRoot
    )

    $candidateDir = Join-Path $Root "meta\memory-candidates"
    if (-not (Test-Path -LiteralPath $candidateDir -PathType Container)) {
        return [ordered]@{
            performed = $false
            classification = "not_needed"
        }
    }

    $rootResolved = (Resolve-Path -LiteralPath $Root).Path
    $candidateResolved = (Resolve-Path -LiteralPath $candidateDir).Path
    $relative = Get-RelativePathCompat -BasePath $rootResolved -TargetPath $candidateResolved
    if ($relative.StartsWith("..") -or [System.IO.Path]::IsPathRooted($relative)) {
        throw "Refusing to remove candidate queue outside the demo root."
    }

    Remove-Item -LiteralPath $candidateResolved -Recurse -Force
    return [ordered]@{
        performed = $true
        classification = "transient_demo_candidate_queue_removed_before_backup"
        path = "meta/memory-candidates"
    }
}

function Get-ForbiddenRealVaultPath {
    $vaultName = -join @([char]0x6211, [char]0x7684, [char]0x5916, [char]0x8111)
    $driveRoot = "F:" + [System.IO.Path]::DirectorySeparatorChar
    return [System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine($driveRoot, $vaultName)
    )
}

function Test-ForbiddenRealVaultPath([string]$Path) {
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/")
    $forbidden = (Get-ForbiddenRealVaultPath).TrimEnd("\", "/")
    if ($candidate.Equals($forbidden, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $forbiddenWithSeparator = $forbidden + [System.IO.Path]::DirectorySeparatorChar
    return $candidate.StartsWith($forbiddenWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)
}

$ScriptDir = Split-Path -Parent $PSCommandPath
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path
$script:RepoRoot = $RepoRoot
$script:IsolatedBase = ""
$DefaultDemoRootResolved = (Resolve-Path -LiteralPath (Join-Path $RepoRoot "examples\demo-root")).Path
$script:DefaultDemoRootResolved = $DefaultDemoRootResolved
$SourceFixtureResolved = (Resolve-Path -LiteralPath (Join-Path $RepoRoot "examples\demo-story")).Path

$CandidateDemoRoot = Resolve-FromRepo $DemoRoot
$CandidateDemoRootFull = [System.IO.Path]::GetFullPath($CandidateDemoRoot)
$UseCustomRoot = $CandidateDemoRootFull -ne $DefaultDemoRootResolved
if ($UseCustomRoot -and (-not $AllowCustomRoot)) {
    throw "Non-demo roots require -AllowCustomRoot. The default safe demo uses a temp synthetic root built from examples\demo-story."
}
if ($UseCustomRoot -and (Test-ForbiddenRealVaultPath $CandidateDemoRootFull)) {
    throw "Custom demo roots must not target the forbidden real user vault path."
}
if ($UseCustomRoot -and (Test-Path -LiteralPath $CandidateDemoRootFull -PathType Leaf)) {
    throw "Custom demo roots must be empty or missing disposable paths."
}
if ($UseCustomRoot -and (Test-Path -LiteralPath $CandidateDemoRootFull -PathType Container)) {
    $existingEntries = @(Get-ChildItem -LiteralPath $CandidateDemoRootFull -Force)
    if ($existingEntries.Count -gt 0) {
        throw "Custom demo roots must be empty or missing disposable paths."
    }
}

if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $ReportPath = Join-Path ([System.IO.Path]::GetTempPath()) "local-llm-wiki-demo\demo-report.json"
}
$ReportFile = Resolve-FromRepo $ReportPath
$ReportDir = Split-Path -Parent $ReportFile
if (-not [string]::IsNullOrWhiteSpace($ReportDir)) {
    New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null
}

$IsolatedBase = Join-Path ([System.IO.Path]::GetTempPath()) ("local-llm-wiki-demo-state-" + [System.Guid]::NewGuid().ToString("N"))
$script:IsolatedBase = $IsolatedBase
$IsolatedAppData = Join-Path $IsolatedBase "config"
$IsolatedLocalAppData = Join-Path $IsolatedBase "local-config"
New-Item -ItemType Directory -Path $IsolatedAppData -Force | Out-Null
New-Item -ItemType Directory -Path $IsolatedLocalAppData -Force | Out-Null

if ($UseCustomRoot) {
    $WorkingRoot = $CandidateDemoRootFull
}
else {
    $WorkingRoot = Join-Path $IsolatedBase "synthetic-root"
}
$BackupPath = Join-Path $IsolatedBase "synthetic-demo-backup.zip"
$RestoreRoot = Join-Path $IsolatedBase "restored-root"

$demoRootBefore = Get-TreeFingerprint $DefaultDemoRootResolved
$demoStoryBefore = Get-TreeFingerprint $SourceFixtureResolved

$sourceFiles = @(Get-ChildItem -LiteralPath $SourceFixtureResolved -Filter "*.md" -File | Sort-Object Name | Select-Object -First 3)
if ($sourceFiles.Count -lt 3) {
    throw "examples\demo-story must contain at least three Markdown source files."
}

$steps = New-Object System.Collections.Generic.List[object]
$ingestedSources = New-Object System.Collections.Generic.List[object]

$step = Invoke-DemoCommand -Name "init-temp-root" -Arguments @("-B", "-m", "kb", "init", "--root", $WorkingRoot) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)

for ($index = 0; $index -lt $sourceFiles.Count; $index++) {
    $number = $index + 1
    $source = $sourceFiles[$index]
    $step = Invoke-DemoCommand -Name "ingest-source-$number" -Arguments @("-B", "-m", "kb", "ingest", $source.FullName, "--root", $WorkingRoot) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
    Assert-DemoStepSucceeded $step
    $steps.Add($step)
    $sourceId = Get-RegexValue -Text $step.stdout_summary -Pattern 'src-[0-9a-f]{12}' -Label "source id"
    $ingestedSources.Add([ordered]@{
        source_id = $sourceId
        fixture_path = ConvertTo-RepoDisplayPath $source.FullName
        review_status = "pending"
    })
}

for ($index = 0; $index -lt $ingestedSources.Count; $index++) {
    $number = $index + 1
    $source = $ingestedSources[$index]
    $step = Invoke-DemoCommand -Name "review-source-$number" -Arguments @("-B", "-m", "kb", "review-source", $source.source_id, "--root", $WorkingRoot, "--status", "reviewed", "--reviewer", "synthetic-demo", "--note", "Synthetic public demo source reviewed for local story run.") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
    Assert-DemoStepSucceeded $step
    $steps.Add($step)
    $source.review_status = "reviewed"
}

$step = Invoke-DemoCommand -Name "search-local-evidence" -Arguments @("-B", "-m", "kb", "search", "offline local workflow safe public release", "--root", $WorkingRoot, "--limit", "3") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)

$step = Invoke-DemoCommand -Name "answer-with-local-evidence" -Arguments @("-B", "-m", "kb", "answer", "What supports the offline local workflow?", "--root", $WorkingRoot, "--limit", "3") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)

$step = Invoke-DemoCommand -Name "capture-candidate" -Arguments @("-B", "-m", "kb", "capture-candidate", "--root", $WorkingRoot, "--type", "preference", "--text", "Synthetic demo operators keep provider calls off by default.", "--event-date", "2026-07-09", "--privacy", "public", "--confidence", "confirmed", "--value-reason", "Documents a public demo governance preference.", "--suggested-source-type", "self_statement") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)
$candidateId = Get-RegexValue -Text $step.stdout_summary -Pattern 'mem-[0-9a-f]{16}' -Label "candidate id"

$step = Invoke-DemoCommand -Name "review-candidate" -Arguments @("-B", "-m", "kb", "review-candidate", $candidateId, "--root", $WorkingRoot, "--status", "approved") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)

$step = Invoke-DemoCommand -Name "publish-memory" -Arguments @("-B", "-m", "kb", "publish-memory", $candidateId, "--root", $WorkingRoot, "--confirm") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)
$publishedMemorySourceId = Get-RegexValue -Text $step.stdout_summary -Pattern 'src-[0-9a-f]{12}' -Label "published memory source id"

$draft = Write-DeterministicDraft -Root $WorkingRoot -SourceId $ingestedSources[0].source_id
$step = New-DemoStoryStep -Name "write-deterministic-draft" -Classification "deterministic_local_draft_written" -Summary "Wrote wiki/_drafts/synthetic-demo-story.md with exact local quote evidence."
$steps.Add($step)

$step = Invoke-DemoCommand -Name "validate-draft" -Arguments @("-B", "-m", "kb", "validate-draft", "--root", $WorkingRoot, $draft.draft_path, "--target", $draft.target) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)

$step = Invoke-DemoCommand -Name "publish-draft" -Arguments @("-B", "-m", "kb", "publish-draft", "--root", $WorkingRoot, $draft.draft_path, "--target", $draft.target) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $step
$steps.Add($step)

$trustStep = Invoke-DemoCommand -Name "trust-report" -Arguments @("-B", "-m", "kb", "trust-report", "--root", $WorkingRoot, "--json") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $trustStep
$steps.Add($trustStep)

$governStep = Invoke-DemoCommand -Name "govern" -Arguments @("-B", "-m", "kb", "govern", "--root", $WorkingRoot) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $governStep
$steps.Add($governStep)

$candidateCleanup = Remove-DemoOnlyCandidateQueue -Root $WorkingRoot -UseCustomRoot $UseCustomRoot

$backupStep = Invoke-DemoCommand -Name "backup" -Arguments @("-B", "-m", "kb", "backup", "--root", $WorkingRoot, "--output", $BackupPath) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $backupStep
$steps.Add($backupStep)

$restoreStep = Invoke-DemoCommand -Name "restore" -Arguments @("-B", "-m", "kb", "restore", "--backup", $BackupPath, "--root", $RestoreRoot) -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $restoreStep
$steps.Add($restoreStep)

$migrateStep = Invoke-DemoCommand -Name "migrate-check" -Arguments @("-B", "-m", "kb", "migrate-check", "--source", $WorkingRoot, "--restored", $RestoreRoot, "--json") -AppDataRoot $IsolatedAppData -LocalAppDataRoot $IsolatedLocalAppData
Assert-DemoStepSucceeded $migrateStep
$steps.Add($migrateStep)

$demoRootAfter = Get-TreeFingerprint $DefaultDemoRootResolved
$demoStoryAfter = Get-TreeFingerprint $SourceFixtureResolved
$trackedFixturesMutated = -not ((Test-TreeFingerprintEqual -Before $demoRootBefore -After $demoRootAfter) -and (Test-TreeFingerprintEqual -Before $demoStoryBefore -After $demoStoryAfter))

$boundaries = [ordered]@{
    offline = $true
    synthetic_data = $true
    writes_real_user_state = $false
    provider_environment_cleared = $true
    tracked_fixtures_mutated = $trackedFixturesMutated
    no_provider_calls = $true
    no_real_user_vault = $true
    redaction_applied = $true
}

$report = [ordered]@{
    schema_version = "synthetic-demo-story-v1"
    demo_root = ConvertTo-DisplayRoot $UseCustomRoot
    source_fixture = "examples/demo-story"
    boundaries = $boundaries
    offline = $true
    synthetic_data = $true
    provider_environment_cleared = $true
    writes_real_user_state = $false
    tracked_fixtures_mutated = $trackedFixturesMutated
    no_provider_calls = $true
    no_real_user_vault = $true
    redaction_applied = $true
    summary = "Synthetic demo story executed in a temp root with local commands only. Nonzero exits are not hidden."
    story_steps = $steps
    ingested_sources = $ingestedSources
    candidate_memory = [ordered]@{
        candidate_id = $candidateId
        status = "published"
        published_source_id = $publishedMemorySourceId
        source_type = "self_statement"
        privacy = "public"
    }
    deterministic_draft = [ordered]@{
        draft_path = $draft.draft_path
        published_path = $draft.published_path
        target = $draft.target
        source_id = $draft.source_id
        evidence_quote = $draft.evidence_quote
    }
    trust_report = [ordered]@{
        status = $trustStep.status
        classification = $trustStep.classification
    }
    governance = [ordered]@{
        exit_code = $governStep.exit_code
        status = $governStep.status
        classification = $(if ($governStep.exit_code -eq 0) { "governance_no_blocking_issues" } else { "governance_reported_blockers" })
    }
    backup_restore_migrate = [ordered]@{
        candidate_queue_cleanup = $candidateCleanup
        backup_status = $backupStep.status
        backup_classification = $backupStep.classification
        restore_status = $restoreStep.status
        restore_classification = $restoreStep.classification
        migrate_status = $migrateStep.status
        migrate_classification = $migrateStep.classification
    }
}

$jsonReport = $report | ConvertTo-Json -Depth 20
Write-Utf8NoBom -Path $ReportFile -Text ($jsonReport + "`n")
Write-Output "Synthetic demo report written."
