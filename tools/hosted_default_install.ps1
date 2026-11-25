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
$fixture = Join-Path $env:ProgramData ('AWQM-Installer-' + $nonce)
$results = Join-Path $fixture 'result'
$report = Join-Path $results 'default-install.json'
$installerFile = Join-Path $fixture 'AWQM-Setup.exe'
$helper = Join-Path $fixture 'installed_app_validation.py'
$expectedInstaller = Join-Path $env:GITHUB_WORKSPACE 'installer\AWQM-Setup.exe'
if ((Get-Item -LiteralPath $Installer).FullName -ne $expectedInstaller) { throw 'Expected the downloaded AWQM installer.' }
$python = (Get-Item -LiteralPath (Join-Path $env:pythonLocation 'python.exe') -ErrorAction Stop).FullName
# Windows loads the new user's HKCU, creates its environment, and grants it
# access to the inherited desktop. Keep the returned handle to check real exit.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class AwqmInstallerTrial {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct StartupInfo {
        public int size;
        public IntPtr reserved, desktop, title;
        public uint x, y, width, height, cols, rows, fill, flags;
        public ushort showWindow, reservedSize;
        public IntPtr reservedPtr, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo {
        public IntPtr process, thread;
        public uint pid, tid;
    }
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
    static extern bool LogonUserW(string user, string domain, string password, uint type, uint provider, out SafeAccessTokenHandle token);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
    static extern bool CreateProcessWithTokenW(SafeAccessTokenHandle token, uint logonFlags, string exe, StringBuilder command,
        uint creationFlags, IntPtr environment, string directory, ref StartupInfo startup, out ProcessInfo info);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetExitCodeProcess(SafeProcessHandle process, out uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool TerminateProcess(SafeProcessHandle process, uint code);

    public static SafeProcessHandle Start(string user, string password, string exe, string directory, out uint pid) {
        const string prefix = "AWQM-Installer-";
        string name = Path.GetFileName(directory);
        Guid fixtureId;
        string expectedPython = Path.Combine(Environment.GetEnvironmentVariable("pythonLocation"), "python.exe");
        if (!name.StartsWith(prefix, StringComparison.Ordinal) ||
            !Guid.TryParseExact(name.Substring(prefix.Length), "N", out fixtureId) ||
            user != "AwqmUi" + fixtureId.ToString("N").Substring(0, 12) ||
            !String.Equals(Path.GetDirectoryName(directory), Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), StringComparison.OrdinalIgnoreCase) ||
            !String.Equals(exe, expectedPython, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Unexpected standard-user AWQM validation target");
        string arguments = String.Format("-I \"{0}\" --install-defaults \"{1}\" \"{2}\" {3}",
            Path.Combine(directory, "installed_app_validation.py"), Path.Combine(directory, "AWQM-Setup.exe"),
            Path.Combine(directory, "result", "default-install.json"), user);
        SafeAccessTokenHandle token;
        if (!LogonUserW(user, ".", password, 2, 0, out token))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        using (token) {
            var startup = new StartupInfo { size = Marshal.SizeOf(typeof(StartupInfo)) };
            ProcessInfo info;
            var command = new StringBuilder("\"" + exe + "\" " + arguments);
            if (!CreateProcessWithTokenW(token, 1, exe, command, 0x08000000, IntPtr.Zero, directory, ref startup, out info))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            CloseHandle(info.thread);
            pid = info.pid;
            return new SafeProcessHandle(info.process, true);
        }
    }

    public static uint ExitCode(SafeProcessHandle process) {
        uint code;
        if (!GetExitCodeProcess(process, out code)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return code;
    }
}
'@
$password = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)) + 'aA1!'
$user = New-LocalUser -Name $userName -Password (ConvertTo-SecureString $password -AsPlainText -Force)
$process = $null
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
    [uint32]$childId = 0
    $process = [AwqmInstallerTrial]::Start($userName, $password, $python, $fixture, [ref]$childId)
    Write-Host "Standard-user Python process started, PID $childId"
    $phase = Join-Path $results 'default-install.phase'
    $lastPhase = 'process created'
    $deadline = [DateTime]::UtcNow.AddSeconds(165)
    while (!(Test-Path -LiteralPath $report) -and [DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $phase) {
            $observed = Get-Content -LiteralPath $phase -Raw
            if ($observed -ne $lastPhase) { Write-Host $observed; $lastPhase = $observed }
        }
        $exitCode = [AwqmInstallerTrial]::ExitCode($process)
        if ($exitCode -ne 259 -and !(Test-Path -LiteralPath $report)) {
            throw "Standard-user Python exited 0x$($exitCode.ToString('X8')) without a final receipt at stage '$lastPhase'."
        }
        Start-Sleep -Seconds 1
    }
    if (!(Test-Path -LiteralPath $report)) { throw "Default launch timed out at stage '$lastPhase'." }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (!$result.passed) { throw "Standard-user first launch failed: $($result.error)" }
    $exitDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([AwqmInstallerTrial]::ExitCode($process) -eq 259 -and [DateTime]::UtcNow -lt $exitDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if ([AwqmInstallerTrial]::ExitCode($process) -ne 0) { throw 'Standard-user verifier did not exit successfully.' }
    Write-Host 'Verified a fresh standard-user install: no installer arguments or UI input, dashboard opened, HTTP sample data present, clean application exit.'
} finally {
    if ($null -ne $process) {
        if ([AwqmInstallerTrial]::ExitCode($process) -eq 259) {
            [void][AwqmInstallerTrial]::TerminateProcess($process, 1)
        }
        $process.Dispose()
    }
    Remove-LocalUser -SID $user.SID
}
