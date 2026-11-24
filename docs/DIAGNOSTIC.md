# Local read-only diagnostic trial (v0.4.0-dev)

This is an explicitly enabled, one-shot field-validation aid, not the production web dashboard or service. Do not execute it on any controller without local authorization and an approved read-only account. AWQM's developers have not connected to actual enterprise equipment.

Use a local protected directory (not a share or cloud-synced folder) and a trusted OpenSSH known_hosts file obtained and verified under the organization's procedure. Unknown/changed keys fail closed. No automatic key enrollment.

Example with documentation-only TEST-NET addresses:
```powershell
.\AWQM.exe --collect-once --authorized --host 192.0.2.1 --host 192.0.2.2 --known-hosts C:\Approved\known_hosts --data-dir C:\Approved\AWQM-trial
```

The username and password are prompted in the local console and the password is not persisted. Do not supply secrets via command line arguments or redirect the input. One initial connection per controller, sequential login; shared credential rejection stops further attempts. One bounded read-only round, one command per controller at a time. CPU unknown, denied or >=70% prevents further heavy queries. No debug, configuration change, device reboot, reconnect loop or continuous polling. A command budget of 30 seconds per controller may defer commands. A parse success does not establish actual device compatibility.

Report shows command/parse outcomes, coherent controller coverage, mapped radios and client counts. Real observation records (not raw CLI) may be written only to the selected diagnostic DB; they remain unvalidated. No web listener is launched. Demo data is isolated in a separate database mode. Do not upload the DB or any real output to public GitHub.

The production system (HTTPS, existing Aruba personal account authorization, DPAPI, continuous service, full adaptive diagnostics) is not complete.
