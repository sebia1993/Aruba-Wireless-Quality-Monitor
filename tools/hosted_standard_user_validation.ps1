[CmdletBinding()]
param([Parameter(Mandatory)][string]$Installer,
      [Parameter(Mandatory)][string]$Output,
      [Parameter(Mandatory)][string]$Previous)
$ErrorActionPreference = 'Stop'
if ($env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'This fixture is restricted to disposable GitHub-hosted runners.'
}
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$previousPath = (Resolve-Path -LiteralPath $Previous).Path
$python = (Get-Command python.exe).Source
$name = 'AwqmDesk' + [guid]::NewGuid().ToString('N').Substring(0,8)
$root = Join-Path $env:ProgramData ('AWQM-CI-' + [guid]::NewGuid().ToString('N'))
$receiptDir = Join-Path $root 'results'
$receipt = Join-Path $receiptDir 'result.json'
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$created = $false
# Start-Process with alternate credentials may pass the parent's environment
# to the secondary logon. Use the native API with a NULL environment block,
# so Windows creates the environment from the disposable user's loaded profile.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;

public static class AwqmCiLogon {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo {
        public uint cb;
        public string reserved, desktop, title;
        public uint x, y, xSize, ySize, xChars, yChars, fill, flags;
        public ushort show, reserved2;
        public IntPtr reservedPtr, stdIn, stdOut, stdErr;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInfo {
        public IntPtr process, thread;
        public uint pid, tid;
    }
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcessWithLogonW(
        string user, string domain, IntPtr password, uint logonFlags,
        string application, StringBuilder commandLine, uint creationFlags,
        IntPtr environment, string directory, ref StartupInfo startup, out ProcessInfo info);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static uint Run(string user, string domain, SecureString password,
                           string executable, string arguments, string directory, uint timeout) {
        var startup = new StartupInfo();
        startup.cb = (uint)Marshal.SizeOf(typeof(StartupInfo));
        var command = new StringBuilder("\"" + executable + "\" " + arguments);
        ProcessInfo info;
        IntPtr plain = Marshal.SecureStringToGlobalAllocUnicode(password);
        try {
            if (!CreateProcessWithLogonW(user, domain, plain, 1, executable, command, 0,
                                         IntPtr.Zero, directory, ref startup, out info))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Disposable user logon failed");
        } finally {
            Marshal.ZeroFreeGlobalAllocUnicode(plain);
        }
        CloseHandle(info.thread);
        try {
            uint wait = WaitForSingleObject(info.process, timeout);
            if (wait == 0x102) {
                TerminateProcess(info.process, 1);
                WaitForSingleObject(info.process, 10000);
                throw new TimeoutException("Standard-user test exceeded its deadline");
            }
            if (wait != 0)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Fixture process wait failed");
            uint code;
            if (!GetExitCodeProcess(info.process, out code))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Fixture exit code unavailable");
            return code;
        } finally {
            CloseHandle(info.process);
        }
    }
}
'@
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
    Copy-Item -LiteralPath $previousPath -Destination (Join-Path $root 'AWQM-Setup.exe')
    Copy-Item -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) 'awqm\dpapi.py') -Destination (Join-Path $root 'fixture_dpapi.py')
    & icacls.exe $root /grant ("*" + $sid + ':(OI)(CI)RX') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Fixture read ACL failed' }
    & icacls.exe $receiptDir /grant ("*" + $sid + ':(OI)(CI)M') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Fixture result ACL failed' }
    $args = '"{0}" --hosted-standard-user "{1}" "{2}" "{3}" "{4}"' -f (Join-Path $root 'installed_app_validation.py'),(Join-Path $root 'AWQM.exe'),$receipt,$name,(Join-Path $root 'AWQM-Setup.exe')
    $exitCode = [AwqmCiLogon]::Run($name, $env:COMPUTERNAME, $password, $python, $args, $root, 480000)
    if (!(Test-Path -LiteralPath $receipt)) { throw "No standard-user evidence (exit $exitCode)" }
    Copy-Item -LiteralPath $receipt -Destination $outputPath
    $result = Get-Content -LiteralPath $receipt -Raw -Encoding utf8 | ConvertFrom-Json
    if ($exitCode -ne 0 -or !$result.passed -or !$result.first_install -or
        !$result.same_download_no_reinstall -or !$result.duplicate_window_restored -or
        !$result.start_menu_relaunch -or !$result.uninstall_verified -or
        !$result.v047_upgrade_preserves_settings -or !$result.uninstall_preserves_user_settings -or
        $result.installer_sha256 -ne (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()) {
        $phase = if (Test-Path -LiteralPath (Join-Path $receiptDir 'result.phase')) { Get-Content -LiteralPath (Join-Path $receiptDir 'result.phase') -Raw } else { 'unknown' }
        throw "Standard-user launch qualification failed in phase $phase : $($result.error)"
    }
    Write-Host "Standard-user install, repeat launch and v0.47 upgrade passed; Windows product type $($result.windows_product_type), build $($result.windows_build)."
} finally {
    if (!(Test-Path -LiteralPath $outputPath) -and (Test-Path -LiteralPath $receipt)) {
        Copy-Item -LiteralPath $receipt -Destination $outputPath -ErrorAction SilentlyContinue
    }
    if ($created) { Remove-LocalUser -Name $name -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
