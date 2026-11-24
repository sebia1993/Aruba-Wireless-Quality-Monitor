# CLI validation (developer preview)

Implemented documentation-shaped, aligned single-line table parsers for radio summary, AP database long, AP active details, association and CPU load. All fixtures are invented using TEST-NET addresses and locally administered MACs. Device-specific output support is not established.

Official documentation:
- https://arubanetworking.hpe.com/techdocs/ArubaOS/oxygen/AOS-8%20Command%20Line%20Interface%20Guide/s/show-ap-radio-summary.html
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-active-details.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-database.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-assoc.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-cpuload.htm

Offline inspection:
```
python -m awqm.probe radio path/to/local-radio-output.txt
python -m awqm.probe active path/to/local-active-output.txt
python -m awqm.probe inventory path/to/local-inventory-output.txt
python -m awqm.probe association path/to/local-association-output.txt
python -m awqm.probe cpu path/to/local-cpu-output.txt
```
Default output is counts or CPU only. --detail prints parsed data locally; do not upload it to GitHub. A successful parse is not field validation. Wrapped tables, missing count footers, permission errors and unsupported formats fail closed. No assumptions about TD/TM/TC time windows or Airtime. For physical Radio matching, require unique wired MAC, AAC and band/channel correspondence to an actual Radio ID. Conflicts remain unresolved; missing data does not indicate Down.

Persistent shell and scheduler are isolated development primitives; no live credentials or live collection are exposed in the current launcher.

## v0.3.0 additions

- `AWQM.exe --validate-cli client local-client-table.txt`
- `AWQM.exe --validate-cli stats local-station-stats.txt`
- `AWQM.exe --validate-cli bss local-bss-table.txt`

Client Table expects an aligned table and its documented closing legend; no documented count footer exists, so absence is never a disconnection signal. Health C/R is split into client and radio health. PHY Rate is the last packet rate; never use it as the average rate. Station Stats counters require unique values and comparable epochs; no measurement capability is implied. BSS mapping requires unique AP/channel/band and rejects ambiguous Radio mapping. No live Airtime is enabled. References:
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-dbg-clt-tab.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-dbg-clt-st.htm
- https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/aos8/sh-ap-bss-table.htm
