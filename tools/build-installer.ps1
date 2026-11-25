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
& $Compiler "/DProductVersion=$version" "/DPayload=$payloadDir" "/DServiceArchive=$archive" "/DServiceManifest=$manifestPath" "/O$out" (Join-Path $PSScriptRoot 'windows-installer.iss')
if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath (Join-Path $out 'AWQM-Setup.exe') -PathType Leaf)) {
    throw 'Windows installer build failed.'
}
