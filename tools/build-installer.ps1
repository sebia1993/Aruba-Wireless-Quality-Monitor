[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Payload,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [string]$Compiler
)

$ErrorActionPreference = 'Stop'
$payloadDir = (Resolve-Path -LiteralPath $Payload).Path
$version = (Get-Content -LiteralPath (Join-Path $payloadDir 'VERSION.txt') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Expected a numeric release version.' }
$program = Join-Path $payloadDir 'AWQM.exe'
$exeFiles = @(Get-ChildItem -LiteralPath $payloadDir -Filter '*.exe' -File)
if ($exeFiles.Count -ne 1 -or $exeFiles[0].Name -cne 'AWQM.exe') { throw 'Expected one application executable.' }
$embeddedVersion = (& $program --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $embeddedVersion -cne $version) { throw 'Payload version mismatch.' }

$archive = (Resolve-Path -LiteralPath "AWQM_${version}_Win11_x64.zip").Path
$manifest = (Get-Content -LiteralPath 'SHA256SUMS.txt' -Raw).Trim() -split '\s+'
if ($manifest.Count -ne 2 -or $manifest[1] -cne [IO.Path]::GetFileName($archive) -or
    $manifest[0] -ine (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash) {
    throw 'Offline service package digest mismatch.'
}
if (!$Compiler) {
    $installed = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($installed) {
        $Compiler = $installed.Source
    } else {
        $Compiler = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Inno Setup 6\ISCC.exe'
    }
}
$Compiler = (Resolve-Path -LiteralPath $Compiler).Path
$out = [IO.Directory]::CreateDirectory($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)).FullName
$manifestPath = (Resolve-Path -LiteralPath 'SHA256SUMS.txt').Path
# Embed the Inno Setup package in the single public launch-or-install executable.
$temp = [IO.Directory]::CreateDirectory((Join-Path ([IO.Path]::GetTempPath()) ("awqm-build-" + [guid]::NewGuid().ToString("N")))).FullName
try {
    & $Compiler "/DProductVersion=$version" "/DPayload=$payloadDir" "/DServiceArchive=$archive" "/DServiceManifest=$manifestPath" "/O$temp" (Join-Path $PSScriptRoot 'windows-installer.iss')
    $inner = Join-Path $temp 'AWQM-Setup.exe'
    if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $inner -PathType Leaf)) { throw 'Windows installer build failed.' }
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (!(Test-Path -LiteralPath $csc)) { throw 'Windows .NET Framework compiler missing.' }
    $metadata = Join-Path $temp 'BuildInfo.cs'
    @"
using System.Reflection;
[assembly: AssemblyTitle("Aruba Wireless Quality Monitor")]
[assembly: AssemblyProduct("Aruba Wireless Quality Monitor")]
[assembly: AssemblyVersion("$version.0")]
[assembly: AssemblyFileVersion("$version.0")]
internal static class BuildInfo { internal const string Version = "$version"; }
"@ | Set-Content -LiteralPath $metadata -Encoding utf8
    $source = Join-Path $PSScriptRoot 'download-launcher.cs'
    $manifestFile = Join-Path $PSScriptRoot 'download-launcher.manifest'
    $policyTests = Join-Path $temp 'LauncherPolicyTests.exe'
    & $csc /nologo /target:exe /platform:x64 /optimize+ /r:System.Windows.Forms.dll /main:LauncherPolicyTests "/out:$policyTests" $source $metadata (Join-Path $PSScriptRoot 'download-launcher-tests.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Launcher policy test compilation failed.' }
    & $policyTests
    if ($LASTEXITCODE -ne 0) { throw 'Launcher policy tests failed.' }
    $entry = Join-Path $out 'AWQM.exe'
    & $csc /nologo /target:winexe /platform:x64 /optimize+ /r:System.Windows.Forms.dll "/win32manifest:$manifestFile" "/resource:$inner,AWQM.Setup" "/out:$entry" $source $metadata
    if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'Unified AWQM executable build failed.' }
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force
}
