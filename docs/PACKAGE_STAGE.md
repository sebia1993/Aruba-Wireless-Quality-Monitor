# Offline service package verification

The public Windows distribution is `AWQM-Setup.exe`. An all-users installation also supplies the program archive and its SHA-256 manifest in `%ProgramFiles%\AWQM Setup\Packages\0.45.0`. On an existing service host it stages the external manager in `%ProgramFiles%\AWQM Setup\Candidates\0.45.0`. Both locations are reserved for local administrator use.

Verify the installer against the trusted release checksum and confirm the embedded service ZIP digest with your approved source. The service ZIP and installer have different hashes. With an existing system installation and a new staging destination under an existing protected local directory:

```powershell
$manager = Join-Path $env:ProgramFiles 'AWQM Setup\Candidates\0.45.0\AWQM.exe'
$package = Join-Path $env:ProgramFiles 'AWQM Setup\Packages\0.45.0\AWQM_0.45.0_Win11_x64.zip'
$trustedZipSha256 = Read-Host 'Approved SHA-256 of the service ZIP'
& $manager --stage-package $package --sha256 $trustedZipSha256 --output 'C:\AWQM-Updates\v045-staged'
```

Staging verifies the copied archive, executable, runtime, documentation and member paths and sizes. Inspect `STAGE-RECEIPT.json` in the destination and compare its `version` and `sha256` with the expected package. Success returns `verified_staged_not_installed`. For the operator PC entry point see [the installation guide](../README.md).

For a service upgrade, an authorized local administrator verifies the stopped service's rollback bundle, switches the program using the external manager, then tests startup under the dedicated identity. The manager also supports restoration of program and data after candidate startup. Follow [rollback preparation](UPDATE_SNAPSHOT.md), [program switch](PROGRAM_SWITCH.md) and [post-start rollback](DATA_ROLLBACK.md).

The [Windows Server SCM trial for v0.42](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45541222743) exercised the frozen folder package, the v0.41 baseline, a candidate start and restoration under a dedicated local account. Record the intended site's own Windows 11 service and reboot results using the [target-PC procedure](WINDOWS_SERVICE.md#windows-11-reboot-qualification).
