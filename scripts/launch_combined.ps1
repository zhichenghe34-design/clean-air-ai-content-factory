[CmdletBinding()]
param(
    [string]$MptRoot,
    [string]$MptPython,
    [string]$AppExecutable,
    [string]$AppPython,
    [string]$Ffmpeg,
    [string]$Ffprobe,
    [string]$MaterialRoot,
    [switch]$AgentTestReview,
    [switch]$NoOpen,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$launcher = Join-Path $PSScriptRoot "launch_combined.py"

$packageManifest = Join-Path $projectRoot "PACKAGE-MANIFEST.json"
if (Test-Path -LiteralPath $packageManifest -PathType Leaf) {
    $launcherPython = Join-Path $projectRoot "runtime\python\python.exe"
} else {
    $launcherPython = $env:SHIYI_LAUNCHER_PYTHON
}
if ([string]::IsNullOrWhiteSpace($launcherPython)) {
    $pythonCandidates = @(
        (Join-Path $projectRoot "python\python.exe"),
        (Join-Path $projectRoot "runtime\python\python.exe"),
        (Join-Path $projectRoot ".venv\Scripts\python.exe")
    )
    $launcherPython = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}

if ([string]::IsNullOrWhiteSpace($launcherPython) -or -not (Test-Path -LiteralPath $launcherPython -PathType Leaf)) {
    [ordered]@{
        status = "error"
        code = "LAUNCHER_PYTHON_MISSING"
        message = "The bundled launcher Python is missing; runtime downloads are disabled."
    } | ConvertTo-Json -Compress
    exit 2
}

$arguments = @("-I", "-S", "-B", "-X", "utf8", $launcher, "--project-root", $projectRoot)
if (-not [string]::IsNullOrWhiteSpace($MptRoot)) { $arguments += @("--mpt-root", $MptRoot) }
if (-not [string]::IsNullOrWhiteSpace($MptPython)) { $arguments += @("--mpt-python", $MptPython) }
if (-not [string]::IsNullOrWhiteSpace($AppExecutable)) { $arguments += @("--app-executable", $AppExecutable) }
if (-not [string]::IsNullOrWhiteSpace($AppPython)) { $arguments += @("--app-python", $AppPython) }
if (-not [string]::IsNullOrWhiteSpace($Ffmpeg)) { $arguments += @("--ffmpeg", $Ffmpeg) }
if (-not [string]::IsNullOrWhiteSpace($Ffprobe)) { $arguments += @("--ffprobe", $Ffprobe) }
if (-not [string]::IsNullOrWhiteSpace($MaterialRoot)) { $arguments += @("--material-root", $MaterialRoot) }
if ($AgentTestReview) { $arguments += "--agent-test-review" }
if ($NoOpen) { $arguments += "--no-open" }
if ($PreflightOnly) { $arguments += "--preflight-only" }

$env:PYTHONUTF8 = "1"
& $launcherPython @arguments
exit $LASTEXITCODE
