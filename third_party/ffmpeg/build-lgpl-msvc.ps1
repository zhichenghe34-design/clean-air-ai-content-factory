[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$BuildRoot,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$VisualStudioRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$FfmpegCommit = "d3ad8a7fee6a647c6362e4a105d949282d50a98f"
$SourceArchiveName = "FFmpeg-$FfmpegCommit.tar.gz"
$SourceArchiveSha256 = "6f7b70d14dbf30b14c2dd78423b289fdceef04e22d3ba7201ffb12066a6ec53b"
$MsysArchiveName = "msys2-base-x86_64-20260611.tar.xz"
$MsysArchiveSha256 = "a2d047e8ee213c3c6a49a8de427eb1069df12207c0422ff1b3cbb5c905c34221"
$MakePackageName = "make-4.4.1-3-x86_64.pkg.tar.zst"
$MakePackageSha256 = "af0bdba17f06fe037f0194069adaa31a8fe45f1a11381501896aea1fae37bd5d"
$NasmPackageName = "nasm-2.16.03-1-x86_64.pkg.tar.zst"
$NasmPackageSha256 = "e5f54d79b94c0290579c20d092603dc97289887ba1c281ac0af88626bfbf1cab"
$DiffutilsPackageName = "diffutils-3.12-1-x86_64.pkg.tar.zst"
$DiffutilsPackageSha256 = "7902c8ce3d4dd69a0f5e98dc9d5c83c17b23314ba486169db57ef6e2835ce3b6"
$ZlibArchiveName = "zlib-1.3.2.tar.gz"
$ZlibArchiveSha256 = "bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16"

$Downloads = Join-Path $BuildRoot "downloads"
$SourceRoot = Join-Path $BuildRoot "src\FFmpeg-FFmpeg-d3ad8a7"
$ZlibSourceRoot = Join-Path $BuildRoot "src\zlib-1.3.2"
$ZlibPrefix = Join-Path $BuildRoot "zlib-prefix"
$Prefix = Join-Path $BuildRoot "prefix"
$Logs = Join-Path $BuildRoot "logs"
$MsysRoot = Join-Path $BuildRoot "msys64"
$Bash = Join-Path $MsysRoot "usr\bin\bash.exe"
$Pacman = Join-Path $MsysRoot "usr\bin\pacman.exe"
$VsDevCmd = Join-Path $VisualStudioRoot "Common7\Tools\VsDevCmd.bat"
$Patch = Join-Path $PSScriptRoot "patches\0001-localized-msvc-detection.patch"

function Assert-FileSha256 {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Expected
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required build input is missing: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected) {
        throw "SHA-256 mismatch for ${Path}: $actual != $Expected"
    }
}

function Convert-ToMsysPath {
    param([Parameter(Mandatory)] [string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($resolved -notmatch '^([A-Za-z]):\\(.*)$') {
        throw "MSYS path conversion only supports drive-qualified Windows paths: $resolved"
    }
    return "/$($Matches[1].ToLowerInvariant())/$($Matches[2].Replace('\', '/'))"
}

function Invoke-Bash {
    param([Parameter(Mandatory)] [string]$Command)
    $wrapped = 'export PATH=/usr/local/bin:/usr/bin:/bin:$PATH' + "`n" + $Command
    & $Bash --noprofile --norc -lc $wrapped
    if ($LASTEXITCODE -ne 0) {
        throw "MSYS2 command failed with exit code $LASTEXITCODE"
    }
}

foreach ($directory in @($BuildRoot, $Downloads, $Prefix, $Logs)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

Assert-FileSha256 (Join-Path $Downloads $SourceArchiveName) $SourceArchiveSha256
Assert-FileSha256 (Join-Path $Downloads $MsysArchiveName) $MsysArchiveSha256
Assert-FileSha256 (Join-Path $Downloads $MakePackageName) $MakePackageSha256
Assert-FileSha256 (Join-Path $Downloads $NasmPackageName) $NasmPackageSha256
Assert-FileSha256 (Join-Path $Downloads $DiffutilsPackageName) $DiffutilsPackageSha256
Assert-FileSha256 (Join-Path $Downloads $ZlibArchiveName) $ZlibArchiveSha256

if (-not (Test-Path -LiteralPath $Bash -PathType Leaf)) {
    & tar.exe -xJf (Join-Path $Downloads $MsysArchiveName) -C $BuildRoot
    if ($LASTEXITCODE -ne 0) { throw "Cannot extract the locked MSYS2 archive" }
}

$env:CHERE_INVOKING = "1"
$env:MSYSTEM = "MSYS"
$env:VSLANG = "1033"
if (@("make.exe", "nasm.exe", "cmp.exe").Where({
    -not (Test-Path -LiteralPath (Join-Path $MsysRoot "usr\bin\$_") -PathType Leaf)
}).Count -gt 0) {
    & $Pacman -U --noconfirm `
        (Join-Path $Downloads $MakePackageName) `
        (Join-Path $Downloads $NasmPackageName) `
        (Join-Path $Downloads $DiffutilsPackageName)
    if ($LASTEXITCODE -ne 0) { throw "Cannot install the locked MSYS2 build packages" }
}

if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot "configure") -PathType Leaf)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot "src") | Out-Null
    & tar.exe -xzf (Join-Path $Downloads $SourceArchiveName) -C (Join-Path $BuildRoot "src")
    if ($LASTEXITCODE -ne 0) { throw "Cannot extract the locked FFmpeg source archive" }
}
if (-not (Test-Path -LiteralPath (Join-Path $ZlibSourceRoot "zlib.h") -PathType Leaf)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $BuildRoot "src") | Out-Null
    & tar.exe -xzf (Join-Path $Downloads $ZlibArchiveName) -C (Join-Path $BuildRoot "src")
    if ($LASTEXITCODE -ne 0) { throw "Cannot extract the locked zlib source archive" }
}

if (-not (Test-Path -LiteralPath $VsDevCmd -PathType Leaf)) {
    throw "Visual Studio developer environment is missing: $VsDevCmd"
}
if (-not (Test-Path -LiteralPath $Patch -PathType Leaf)) {
    throw "Required FFmpeg source patch is missing: $Patch"
}

$Git = (Get-Command git.exe -ErrorAction Stop).Source
$savedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Git -C $SourceRoot apply --check $Patch 2>&1 | Out-Null
    $canApply = $LASTEXITCODE -eq 0
    if ($canApply) {
        & $Git -C $SourceRoot apply $Patch 2>&1 | Out-Null
        $patchReady = $LASTEXITCODE -eq 0
    } else {
        & $Git -C $SourceRoot apply --reverse --check $Patch 2>&1 | Out-Null
        $patchReady = $LASTEXITCODE -eq 0
    }
} finally {
    $ErrorActionPreference = $savedErrorActionPreference
}
if (-not $patchReady) {
    throw "FFmpeg source is neither pristine nor patched exactly as expected"
}

# Import the exact x64 MSVC/Windows SDK environment into this PowerShell process.
$environmentLines = & $env:ComSpec /d /s /c "`"$VsDevCmd`" -no_logo -arch=amd64 -host_arch=amd64 >nul && set"
if ($LASTEXITCODE -ne 0) { throw "VsDevCmd failed with exit code $LASTEXITCODE" }
foreach ($line in $environmentLines) {
    $separator = $line.IndexOf('=')
    if ($separator -le 0) { continue }
    $name = $line.Substring(0, $separator)
    $value = $line.Substring($separator + 1)
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}
$env:CHERE_INVOKING = "1"
$env:MSYSTEM = "MSYS"
$env:VSLANG = "1033"

# Build the only non-system external runtime dependency as an MSVC DLL. FFmpeg
# links to the import library, so the LGPL FFmpeg DLLs remain replaceable.
$zlibCFlags = "-nologo -MT -W3 -O2 -Oy-"
$zlibLdFlags = "-nologo -incremental:no -opt:ref"
$zlibBuildLog = Join-Path $Logs "zlib-build.log"
$zlibOutput = @()
$savedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
Push-Location $ZlibSourceRoot
try {
    $zlibOutput += & nmake.exe -nologo -f win32/Makefile.msc clean 2>&1
    if ($LASTEXITCODE -ne 0) { throw "zlib clean failed with exit code $LASTEXITCODE" }
    $zlibOutput += & nmake.exe -nologo -f win32/Makefile.msc `
        "CFLAGS=$zlibCFlags" "LDFLAGS=$zlibLdFlags" zlib1.dll 2>&1
    if ($LASTEXITCODE -ne 0) { throw "zlib DLL build failed with exit code $LASTEXITCODE" }
    $zlibOutput += & nmake.exe -nologo -f win32/Makefile.msc `
        "CFLAGS=$zlibCFlags" "LDFLAGS=$zlibLdFlags" testdll 2>&1
    if ($LASTEXITCODE -ne 0) { throw "zlib DLL tests failed with exit code $LASTEXITCODE" }
} finally {
    $zlibOutput | Set-Content -LiteralPath $zlibBuildLog -Encoding utf8
    $ErrorActionPreference = $savedErrorActionPreference
    Pop-Location
}
foreach ($directory in @(
    (Join-Path $ZlibPrefix "bin"),
    (Join-Path $ZlibPrefix "include"),
    (Join-Path $ZlibPrefix "lib")
)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
Copy-Item -LiteralPath (Join-Path $ZlibSourceRoot "zlib1.dll") -Destination (Join-Path $ZlibPrefix "bin\zlib1.dll") -Force
Copy-Item -LiteralPath (Join-Path $ZlibSourceRoot "zdll.lib") -Destination (Join-Path $ZlibPrefix "lib\zlib.lib") -Force
Copy-Item -LiteralPath (Join-Path $ZlibSourceRoot "zlib.h") -Destination (Join-Path $ZlibPrefix "include\zlib.h") -Force
Copy-Item -LiteralPath (Join-Path $ZlibSourceRoot "zconf.h") -Destination (Join-Path $ZlibPrefix "include\zconf.h") -Force

$sourceMsys = Convert-ToMsysPath $SourceRoot
$prefixMsys = Convert-ToMsysPath $Prefix
$logsMsys = Convert-ToMsysPath $Logs
$zlibInclude = ((Join-Path $ZlibPrefix "include").Replace('\', '/'))
$zlibLib = ((Join-Path $ZlibPrefix "lib").Replace('\', '/'))

$configureFlags = @(
    "--prefix=$prefixMsys",
    "--toolchain=msvc",
    "--arch=x86_64",
    "--target-os=win64",
    "--enable-shared",
    "--disable-static",
    "--disable-debug",
    "--disable-doc",
    "--disable-ffplay",
    "--disable-autodetect",
    "--disable-network",
    "--disable-avdevice",
    "--enable-w32threads",
    "--enable-zlib",
    "--enable-ffmpeg",
    "--enable-ffprobe",
    "--enable-mediafoundation",
    "--enable-d3d11va",
    "--enable-encoder=h264_mf",
    "--enable-encoder=aac",
    "--extra-cflags=-I$zlibInclude",
    "--extra-ldflags=-libpath:$zlibLib"
)

$configureCommand = ($configureFlags -join ' ')
$msvcClMsys = Convert-ToMsysPath (Get-Command cl.exe -ErrorAction Stop).Source
$msvcLinkMsys = Convert-ToMsysPath (Get-Command link.exe -ErrorAction Stop).Source
$toolRecord = @(
    "set -euo pipefail",
    "cd '$sourceMsys'",
    "{",
    "  printf 'ffmpeg_commit=%s\n' '$FfmpegCommit'",
    "  printf 'source_archive_sha256=%s\n' '$SourceArchiveSha256'",
    "  printf 'msys2_archive=%s\n' '$MsysArchiveName'",
    "  printf 'msys2_archive_sha256=%s\n' '$MsysArchiveSha256'",
    "  printf 'zlib_cflags=%s\n' '$zlibCFlags'",
    "  printf 'zlib_ldflags=%s\n' '$zlibLdFlags'",
    "  uname -a",
    "  make --version | head -n 1",
    "  nasm -v",
    "  cmp --version | head -n 1",
    "  { '$msvcClMsys' 2>&1 || true; } | head -n 3",
    "  { '$msvcLinkMsys' 2>&1 || true; } | head -n 3",
    "} > '$logsMsys/tool-versions.txt'"
) -join "`n"
Invoke-Bash $toolRecord

$configureScript = @(
    "set -euo pipefail",
    "cd '$sourceMsys'",
    "if test -f ffbuild/config.mak; then make distclean; fi",
    "./configure $configureCommand 2>&1 | tee '$logsMsys/configure.log'",
    "test `${PIPESTATUS[0]} -eq 0",
    "cp ffbuild/config.log '$logsMsys/ffbuild-config.log'",
    "cp config.h '$logsMsys/config.h'",
    "cp config_components.h '$logsMsys/config_components.h'"
) -join "`n"
Invoke-Bash $configureScript

$parallelism = [Math]::Max(1, [Math]::Min(16, [Environment]::ProcessorCount))
$buildScript = @(
    "set -euo pipefail",
    "cd '$sourceMsys'",
    "make -j$parallelism 2>&1 | tee '$logsMsys/build.log'",
    "test `${PIPESTATUS[0]} -eq 0",
    "make install 2>&1 | tee '$logsMsys/install.log'",
    "test `${PIPESTATUS[0]} -eq 0"
) -join "`n"
Invoke-Bash $buildScript

Copy-Item -LiteralPath (Join-Path $ZlibPrefix "bin\zlib1.dll") -Destination (Join-Path $Prefix "bin\zlib1.dll") -Force

$configuration = & (Join-Path $Prefix "bin\ffmpeg.exe") -hide_banner -buildconf 2>&1
$configuration | Set-Content -LiteralPath (Join-Path $Logs "ffmpeg-buildconf.txt") -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "The newly built ffmpeg.exe cannot report its configuration" }

Write-Host "FFmpeg LGPL MSVC shared runtime built at $Prefix"
