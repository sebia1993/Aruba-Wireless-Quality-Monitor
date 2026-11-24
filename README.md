# Aruba Wireless Quality Monitor (AWQM)

Windows 11 / ArubaOS 8 클러스터 기반 무선 품질 분석 도구.

**현재 단계: v0.4.0-dev — 로컬 수집 진단 및 DB 저장·복구 기반 개발. 운영 배포 금지.**
실제 장비 접속, 실측 Airtime, 72시간 구동, Windows Service, HTTPS 및 Aruba 개인 계정 인증은 아직 검증/완료되지 않았습니다. 모의 화면은 실제 측정값이 아닙니다.

## 실행 (개발 환경)
```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python launcher.py --demo
```
브라우저에서 http://127.0.0.1:8765 를 확인합니다. 명시적 --demo 옵션이 있어야 모의 화면이 실행됩니다. 루프백 전용입니다.

## 오프라인 CLI 파서 확인
```sh
python launcher.py --validate-cli radio local-output.txt
```
Windows EXE에서도 동일하게 `AWQM.exe --validate-cli radio local-output.txt`를 사용할 수 있습니다. 명령 종류: radio, inventory, active, association, cpu, client, stats, bss. 기본 결과는 건수 또는 CPU 값이며 SSH에 연결하지 않습니다. 실제 CLI 결과는 사내 PC 밖으로 전송하지 않습니다. --detail 출력에는 식별 정보가 포함될 수 있으므로 주의하십시오.

## 승인된 현장 검증용 1회 진단 (선택)

`AWQM.exe --collect-once --help`로 옵션을 확인합니다. 실제 접속은 운영자가 승인된 관리 IP와 검증된 known_hosts, 조회 계정을 제공하고 명시적으로 실행할 때만 수행합니다. 기존 승인된 운영 절차에 따르십시오. 진단 모드는 웹 서버를 열지 않으며, 실제 CLI/장비 호환성은 아직 미검증입니다. 자세한 절차는 [현장 진단 안내](docs/DIAGNOSTIC.md) 참고.

## Windows 빌드
```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --name AWQM --add-data "awqm/static;awqm/static" launcher.py
dist\AWQM\AWQM.exe --demo
```
GitHub Actions의 Windows ZIP artifact는 개발용입니다. 정식 릴리스로 간주하지 않습니다.

- [확정 설계 기준 및 개발 범위](docs/DESIGN.md)
- [CLI 출력 검증 안내](docs/CLI_VALIDATION.md)
- [작업 현황 및 검증 게이트](docs/STATUS.md)
- [DB 저장 및 복구 설계](docs/STORAGE.md)

실제 계정, AP 이름, IP, SSID, CLI 원본, DB 및 인증서를 공개 저장소에 올리지 않습니다.
