param(
    [string]$TextFile,
    [string]$OutputFile,
    [int]$Rate = -1,
    [string]$Language = "zh-CN",
    [switch]$ProbeOnly
)

$ErrorActionPreference = 'Stop'
$text = $null
$target = $null
if (-not $ProbeOnly) {
    if ([System.String]::IsNullOrWhiteSpace($TextFile) -or [System.String]::IsNullOrWhiteSpace($OutputFile)) {
        throw "TextFile and OutputFile are required unless ProbeOnly is used"
    }
    $resolvedText = (Resolve-Path -LiteralPath $TextFile).Path
    $target = [System.IO.Path]::GetFullPath($OutputFile)
    $text = [System.IO.File]::ReadAllText($resolvedText, [System.Text.Encoding]::UTF8)
    if ([System.String]::IsNullOrWhiteSpace($text)) {
        throw "SAPI input text must not be empty"
    }
}

Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $speaker.GetInstalledVoices() |
        Where-Object {
            $_.Enabled -and
            $_.VoiceInfo.Culture -and
            $_.VoiceInfo.Culture.Name.Equals($Language, [System.StringComparison]::OrdinalIgnoreCase)
        } |
        Select-Object -First 1
    if ($null -eq $voice) {
        throw "Required offline SAPI voice is not installed: $Language"
    }
    if ($ProbeOnly) {
        Write-Output ("SAPI_VOICE={0};CULTURE={1}" -f $voice.VoiceInfo.Name, $voice.VoiceInfo.Culture.Name)
        return
    }
    $targetDirectory = [System.IO.Path]::GetDirectoryName($target)
    if (-not [System.IO.Directory]::Exists($targetDirectory)) {
        [System.IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
    }
    $speaker.SelectVoice($voice.VoiceInfo.Name)
    $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
    $speaker.SetOutputToWaveFile($target)
    $speaker.Speak($text)
    Write-Output ("SAPI_VOICE={0};CULTURE={1}" -f $voice.VoiceInfo.Name, $voice.VoiceInfo.Culture.Name)
}
finally {
    $speaker.Dispose()
}

if (-not (Test-Path -LiteralPath $target) -or (Get-Item -LiteralPath $target).Length -le 44) {
    throw "SAPI did not create a valid WAV file: $target"
}
