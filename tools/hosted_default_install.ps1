# Run the normal installer with no input in a disposable standard-user profile.
# Every process checking windows or HTTP runs on the same desktop as the installer.
param([Parameter(Mandatory)][string]$Installer)
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or
    $env:GITHUB_JOB -ne 'installed-app') {
    throw 'A disposable GitHub-hosted installed-app job is required.'
}
$nonce = [guid]::NewGuid().ToString('N')
$userName = 'AwqmUi' + $nonce.Substring(0, 12)
$taskName = 'AWQM-Installer-' + $nonce
$fixture = Join-Path $env:ProgramData $taskName
$results = Join-Path $fixture 'result'
$report = Join-Path $results 'default-install.json'
$installerFile = Join-Path $fixture 'AWQM-Setup.exe'
$helper = Join-Path $fixture 'installed_app_validation.py'
$python = (Get-Command python -CommandType Application).Source
$password = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)) + 'aA1!'
$user = New-LocalUser -Name $userName -Password (ConvertTo-SecureString $password -AsPlainText -Force)
$registered = $false
try {
    $admins = Get-LocalGroup -SID 'S-1-5-32-544'
    if (@(Get-LocalGroupMember -Group $admins | Where-Object { $_.SID -eq $user.SID }).Count) {
        throw 'Fixture account unexpectedly belongs to Administrators.'
    }
    $users = Get-LocalGroup -SID 'S-1-5-32-545'
    if (!@(Get-LocalGroupMember -Group $users | Where-Object { $_.SID -eq $user.SID }).Count) {
        Add-LocalGroupMember -Group $users -Member $user
    }
    New-Item -ItemType Directory -Path $fixture -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $Installer -Destination $installerFile
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'installed_app_validation.py') -Destination $helper
    New-Item -ItemType Directory -Path $results -ErrorAction Stop | Out-Null
    $sid = '*' + $user.SID.Value
    icacls $fixture /grant:r ("${sid}:(OI)(CI)RX") | Out-Null
    if ($LASTEXITCODE) { throw 'Could not grant fixture read access.' }
    icacls $results /grant:r ("${sid}:(OI)(CI)M") | Out-Null
    if ($LASTEXITCODE) { throw 'Could not grant fixture report access.' }
    $arguments = '-I "{0}" --install-defaults "{1}" "{2}" {3}' -f $helper, $installerFile, $report, $userName
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $fixture
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
    Register-ScheduledTask -TaskName $taskName -Action $action -Settings $settings -RunLevel Limited -User "$env:COMPUTERNAME\$userName" -Password $password | Out-Null
    $registered = $true
    Start-ScheduledTask -TaskName $taskName
    $deadline = [DateTime]::UtcNow.AddSeconds(165)
    while (!(Test-Path -LiteralPath $report) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds 2
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Ready' -and $info.LastRunTime.Year -gt 2000 -and
            !(Test-Path -LiteralPath $report)) {
            throw "The standard-user task exited without evidence. Task Scheduler result $($info.LastTaskResult)"
        }
    }
    if (!(Test-Path -LiteralPath $report)) { throw 'Timed out waiting for the default installer dashboard.' }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (!$result.passed) { throw "Standard-user first launch failed: $($result.error)" }
    Write-Host 'Verified a fresh standard-user install: no installer arguments or UI input, dashboard opened, HTTP sample data present, clean application exit.'
} finally {
    if ($registered) {
        if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Remove-LocalUser -SID $user.SID
}
