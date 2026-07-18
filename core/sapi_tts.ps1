param(
    [Parameter(Mandatory = $true)][string]$TextFile,
    [Parameter(Mandatory = $true)][string]$OutputFile,
    [int]$Rate = -1
)

$ErrorActionPreference = 'Stop'
$resolvedText = (Resolve-Path -LiteralPath $TextFile).Path
$target = [System.IO.Path]::GetFullPath($OutputFile)
$targetDirectory = [System.IO.Path]::GetDirectoryName($target)
if (-not [System.IO.Directory]::Exists($targetDirectory)) {
    [System.IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
}

Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
    $speaker.SetOutputToWaveFile($target)
    $speaker.Speak([System.IO.File]::ReadAllText($resolvedText, [System.Text.Encoding]::UTF8))
}
finally {
    $speaker.Dispose()
}

if (-not (Test-Path -LiteralPath $target) -or (Get-Item -LiteralPath $target).Length -le 44) {
    throw "SAPI did not create a valid WAV file: $target"
}
