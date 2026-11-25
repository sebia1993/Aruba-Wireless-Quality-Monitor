[CmdletBinding()]
param([Parameter(Mandatory)][string]$Installer,
      [Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
if ($env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'This fixture is restricted to disposable GitHub-hosted runners.'
}
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$python = (Get-Command python.exe).Source
$name = 'AwqmDesk' + [guid]::NewGuid().ToString('N').Substring(0,8)
$root = Join-Path $env:ProgramData ('AWQM-CI-' + [guid]::NewGuid().ToString('N'))
$receiptDir = Join-Path $root 'results'
$receipt = Join-Path $receiptDir 'result.json'
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$created = $false
$proc = $null
try {
    $password = ConvertTo-SecureString (([guid]::NewGuid().ToString('N')) + 'aA1!') -AsPlainText -Force
    $user = New-LocalUser -Name $name -Password $password -Description 'Disposable AWQM CI standard-user fixture'
    $created = $true
    $users = Get-LocalGroup -SID 'S-1-5-32-545'
    Add-LocalGroupMember -Group $users.Name -Member $user -ErrorAction SilentlyContinue
    $sid = $user.SID.Value
    New-Item -ItemType Directory -Path $root,$receiptDir -Force | Out-Null
    Copy-Item -LiteralPath $installerPath -Destination (Join-Path $root 'AWQM.exe')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'installed_app_validation.py') -Destination $root
    & icacls.exe $root /grant ("*" + $sid + ':(OI)(CI)RX') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Fixture read ACL failed' }
    & icacls.exe $receiptDir /grant ("*" + $sid + ':(OI)(CI)M') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Fixture result ACL failed' }
    $credential = [PSCredential]::new("$env:COMPUTERNAME\$name", $password)
    $args = '"{0}" --hosted-standard-user "{1}" "{2}" "{3}"' -f (Join-Path $root 'installed_app_validation.py'),(Join-Path $root 'AWQM.exe'),$receipt,$name
    $proc = Start-Process -FilePath $python -ArgumentList $args -Credential $credential -LoadUserProfile -WorkingDirectory $root -PassThru
    if (!$proc.WaitForExit(360000)) { throw 'Standard-user test exceeded its deadline' }
    if (!(Test-Path -LiteralPath $receipt)) { throw "No standard-user evidence (exit $($proc.ExitCode))" }
    Copy-Item -LiteralPath $receipt -Destination $outputPath
    $result = Get-Content -LiteralPath $receipt -Raw -Encoding utf8 | ConvertFrom-Json
    if ($proc.ExitCode -ne 0 -or !$result.passed -or !$result.first_install -or
        !$result.same_download_no_reinstall -or !$result.duplicate_window_restored -or
        !$result.start_menu_relaunch -or !$result.uninstall_verified -or
        $result.installer_sha256 -ne (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()) {
        throw "Standard-user launch qualification failed: $($result.error)"
    }
    Write-Host "Standard-user no-argument install and repeated launch passed; Windows product type $($result.windows_product_type), build $($result.windows_build)."
} finally {
    if ($proc -and !$proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    if (!(Test-Path -LiteralPath $outputPath) -and (Test-Path -LiteralPath $receipt)) {
        Copy-Item -LiteralPath $receipt -Destination $outputPath -ErrorAction SilentlyContinue
    }
    if ($created) { Remove-LocalUser -Name $name -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
