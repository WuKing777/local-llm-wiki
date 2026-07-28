param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$OutputPath,

  [string]$RepositoryUrl
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd(
  [System.IO.Path]::DirectorySeparatorChar,
  [System.IO.Path]::AltDirectorySeparatorChar
)
$OutputFull = [System.IO.Path]::GetFullPath($OutputPath).TrimEnd(
  [System.IO.Path]::DirectorySeparatorChar,
  [System.IO.Path]::AltDirectorySeparatorChar
)

function Test-IsSameOrChildPath([string]$Candidate, [string]$Parent) {
  if ($Candidate.Equals($Parent, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $true
  }
  $parentWithSeparator = $Parent + [System.IO.Path]::DirectorySeparatorChar
  return $Candidate.StartsWith(
    $parentWithSeparator,
    [System.StringComparison]::OrdinalIgnoreCase
  )
}

function Get-ValidatedRepositoryLocation([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return $null
  }

  $uri = $null
  if (-not [System.Uri]::TryCreate(
    $Value,
    [System.UriKind]::Absolute,
    [ref]$uri
  )) {
    throw "RepositoryUrl must be an absolute HTTPS GitHub repository URL."
  }
  if (
    $uri.Scheme -ne "https" -or
    -not $uri.Host.Equals(
      "github.com",
      [System.StringComparison]::OrdinalIgnoreCase
    ) -or
    -not $uri.IsDefaultPort -or
    -not [string]::IsNullOrEmpty($uri.UserInfo) -or
    -not [string]::IsNullOrEmpty($uri.Query) -or
    -not [string]::IsNullOrEmpty($uri.Fragment)
  ) {
    throw "RepositoryUrl must be an HTTPS github.com URL without credentials, query, fragment, or custom port."
  }

  $decodedPath = [System.Uri]::UnescapeDataString($uri.AbsolutePath)
  if ($decodedPath.Contains("\")) {
    throw "RepositoryUrl contains an unsafe path separator."
  }
  $segments = @($decodedPath.Trim("/") -split "/")
  if (
    $segments.Count -ne 2 -or
    $segments[0] -notmatch "^[A-Za-z0-9][A-Za-z0-9-]{0,38}$" -or
    $segments[1] -notmatch "^[A-Za-z0-9._-]+$"
  ) {
    throw "RepositoryUrl path must contain exactly one safe owner and repository name."
  }

  $owner = $segments[0]
  $repository = $segments[1]
  if ($repository.EndsWith(".git", [System.StringComparison]::OrdinalIgnoreCase)) {
    $repository = $repository.Substring(0, $repository.Length - 4)
  }
  if (
    [string]::IsNullOrWhiteSpace($repository) -or
    $repository -eq "." -or
    $repository -eq ".."
  ) {
    throw "RepositoryUrl contains an invalid repository name."
  }

  $canonical = "https://github.com/$owner/$repository"
  return [PSCustomObject]@{
    CanonicalUrl = $canonical
    CloneUrl = "$canonical.git"
    IssuesUrl = "$canonical/issues"
  }
}

$repositoryLocation = Get-ValidatedRepositoryLocation $RepositoryUrl

if (Test-Path -LiteralPath $OutputFull) {
  throw "Output path already exists. Choose a new directory; this script never removes or overwrites existing output."
}

if (Test-IsSameOrChildPath $OutputFull $RepoRoot) {
  throw "Output path must be outside the source repository."
}

$OutputParent = Split-Path -Parent $OutputFull
if ($OutputParent -and -not (Test-Path -LiteralPath $OutputParent)) {
  New-Item -ItemType Directory -Path $OutputParent | Out-Null
}
New-Item -ItemType Directory -Path $OutputFull | Out-Null

$rootPrivateDirs = @(
  ".git",
  ".obsidian",
  "backups",
  "db",
  "inbox",
  "meta",
  "raw",
  "reports",
  "sources",
  "wiki"
)
$excludedDirectoryNames = @(
  ".eggs",
  ".mypy_cache",
  ".nox",
  ".pytest_cache",
  ".ruff_cache",
  ".tox",
  ".venv",
  "__pycache__",
  "build",
  "dist",
  "env",
  "htmlcov",
  "node_modules",
  "venv"
)
$excludedFileNames = @(
  ".coverage",
  "coverage.xml",
  ".env",
  "id_dsa",
  "id_ecdsa",
  "id_ed25519",
  "id_rsa"
)
$excludedRelativePaths = @(
  "tests/test_personal_exobrain_init.py"
)

function Get-RelativeRepositoryPath([string]$FullPath) {
  return $FullPath.Substring($RepoRoot.Length).TrimStart(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
  )
}

function Test-IsExcludedRelativePath([string]$RelativePath) {
  $parts = @($RelativePath -split "[\\/]" | Where-Object { $_ })
  if ($parts.Count -eq 0) {
    return $true
  }
  $normalizedRelativePath = ($parts -join "/")
  if ($excludedRelativePaths -contains $normalizedRelativePath) {
    return $true
  }
  if ($rootPrivateDirs -contains $parts[0]) {
    return $true
  }
  if ($parts.Count -ge 2 -and $parts[0] -eq "docs" -and $parts[1] -eq "superpowers") {
    return $true
  }
  foreach ($part in $parts) {
    if ($excludedDirectoryNames -contains $part) {
      return $true
    }
    if ($part -like "*.egg-info") {
      return $true
    }
  }
  if ($excludedFileNames -contains $parts[-1]) {
    return $true
  }
  foreach ($pattern in @(".env.*", "*.key", "*.pem", "*.p12", "*.pfx")) {
    if ($parts[-1] -like $pattern) {
      return $true
    }
  }
  return $false
}

$copied = 0
Get-ChildItem -LiteralPath $RepoRoot -Recurse -Force -File | ForEach-Object {
  if ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    return
  }
  $relative = Get-RelativeRepositoryPath $_.FullName
  if (Test-IsExcludedRelativePath $relative) {
    return
  }

  $destination = Join-Path $OutputFull $relative
  $destinationDirectory = Split-Path -Parent $destination
  if (-not (Test-Path -LiteralPath $destinationDirectory)) {
    New-Item -ItemType Directory -Path $destinationDirectory | Out-Null
  }
  Copy-Item -LiteralPath $_.FullName -Destination $destination
  $script:copied += 1
}

if ($null -ne $repositoryLocation) {
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  $repositoryPlaceholder = "<repository" + "-url>"
  $materializedClonePattern = (
    "https://github\.com/" +
    "[A-Za-z0-9][A-Za-z0-9-]{0,38}/" +
    "[A-Za-z0-9._-]+\.git"
  )
  foreach ($relativePath in @(
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "docs/product/installation.md"
  )) {
    $targetPath = Join-Path $OutputFull $relativePath
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
      throw "Release-facing file is missing from export: $relativePath"
    }
    $text = [System.IO.File]::ReadAllText($targetPath)
    if ($text.Contains($repositoryPlaceholder)) {
      $materialized = $text.Replace(
        $repositoryPlaceholder,
        $repositoryLocation.CloneUrl
      )
    } elseif ([System.Text.RegularExpressions.Regex]::IsMatch(
      $text,
      $materializedClonePattern
    )) {
      $materialized = [System.Text.RegularExpressions.Regex]::Replace(
        $text,
        $materializedClonePattern,
        $repositoryLocation.CloneUrl
      )
    } else {
      throw "Release-facing file has neither a repository URL placeholder nor a safe materialized GitHub clone URL: $relativePath"
    }
    if ($materialized.Contains($repositoryPlaceholder)) {
      throw "Repository URL placeholder remains after materialization: $relativePath"
    }
    [System.IO.File]::WriteAllText($targetPath, $materialized, $utf8NoBom)
  }

  $pyprojectPath = Join-Path $OutputFull "pyproject.toml"
  if (-not (Test-Path -LiteralPath $pyprojectPath -PathType Leaf)) {
    throw "pyproject.toml is missing from the public export."
  }
  $pyproject = [System.IO.File]::ReadAllText($pyprojectPath)
  $projectUrls = @"
[project.urls]
Homepage = "$($repositoryLocation.CanonicalUrl)"
Repository = "$($repositoryLocation.CanonicalUrl)"
Issues = "$($repositoryLocation.IssuesUrl)"
"@
  if ($pyproject -match "(?m)^\[project\.urls\]\s*$") {
    $pyproject = [System.Text.RegularExpressions.Regex]::Replace(
      $pyproject.TrimEnd(),
      "(?ms)\r?\n\[project\.urls\]\s*\r?\n.*\z",
      [Environment]::NewLine + $projectUrls.Trim()
    )
  } else {
    $pyproject = $pyproject.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $projectUrls.Trim()
  }
  [System.IO.File]::WriteAllText(
    $pyprojectPath,
    $pyproject.TrimEnd() + [Environment]::NewLine,
    $utf8NoBom
  )
}

Write-Output "Created public export snapshot with $copied files: $OutputFull"
