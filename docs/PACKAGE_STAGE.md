# Offline update preparation

Use the complete folder ZIP for service staging and rollback. The portable ZIP contains only the standalone executable.

Obtain the folder ZIP and confirm its expected SHA-256 through an independently trusted channel. Service operation also requires the Windows 11 and site qualification recorded in the release notes.

```powershell
.\AWQM.exe --stage-package .\AWQM_0.44.0_Win11_x64.zip --sha256 $trustedZipSha256 --output C:\AWQM-Updates\v044-staged
```
Set `$trustedZipSha256` to the independently verified 64-character digest of the folder-package ZIP. Use a new destination under an existing protected local parent. The command verifies the copied archive, checks member paths and sizes, and verifies the executable, runtime and documentation layout. Read `STAGE-RECEIPT.json` in the destination and compare its `version` and `sha256` to the expected package. Successful staging returns `status` equal to `verified_staged_not_installed`. The portable single-executable ZIP has a different layout.

For a service upgrade, an authorized local administrator verifies the stopped service's rollback bundle, switches the program using the external manager, then tests startup under the dedicated identity. The manager also supports restoration of program and data after candidate startup. Follow [rollback preparation](UPDATE_SNAPSHOT.md), [program switch](PROGRAM_SWITCH.md) and [post-start rollback](DATA_ROLLBACK.md).

The [Windows Server SCM trial for v0.42](https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor/actions/runs/45541222743) exercised the frozen folder package, the v0.41 baseline, a candidate start and restoration under a dedicated local account. Record the intended site's own Windows 11 service and reboot results using the [target-PC procedure](WINDOWS_SERVICE.md#windows-11-reboot-qualification).
