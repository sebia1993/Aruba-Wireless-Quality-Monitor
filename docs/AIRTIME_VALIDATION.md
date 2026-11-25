# Client-info Airtime field inspection (v0.36)

Official CLI reference:
https://arubanetworking.hpe.com/techdocs/ArubaOS/oxygen/AOS-8%20Command%20Line%20Interface%20Guide/s/show-ap-debug-client-info.html

The published 802.11ax debug example includes repeated HTT Tx TID statistics
and a tid_tx_airtime field. It does not establish this field's time unit,
cumulative versus instantaneous meaning, reset/wrap rules, TID completeness,
or whether shared MU/OFDMA transmission intervals overlap. Do not sum it,
interpret zero as no airtime, or calculate a percentage from it. Actual
support on AP-535 / ArubaOS 8.10.0.24 remains a site validation requirement.

The offline parser accepts a unique matching client/driver target and
peer-statistics block, unique opaque TID field keys and bounded integer
values. It does not decode the packed key, infer a field meaning, verify
the expected number of TIDs, or certify that the whole command is complete.
The same-context pair check compares the reported peer/interface and key
set and counts increased/decreased/unchanged values only. This is not proof
of association continuity or counter behavior. It produces no identifiers,
raw values or measured/estimated occupancy, even with --detail.

```powershell
AWQM.exe --validate-cli client-info-airtime .\sample1.txt
AWQM.exe --validate-cli client-info-airtime .\sample1.txt --compare-file .\sample2.txt
```

Only use locally authorized output files. Do not upload actual debug output.
This tool never connects to devices. For a future live integration, separately
validate exact firmware/AP model, full layout and key coverage, elapsed time
and association continuity, time units, counter/reset behavior, Tx/Rx scope,
whether MU/OFDMA intervals are shared, and effects on WLC/AP CLI load. Until
that validation is available, live Airtime and RF01/RF02 occupancy attribution
remain unknown. The existing numeric contract now additionally rejects
nonboolean validation flags and counter values outside unsigned 64-bit range.
