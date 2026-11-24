# Aruba Wireless Quality Monitor (AWQM)

Windows 11 / ArubaOS 8 클러스터 기반 무선 품질 분석 도구.

**현재 단계: v0.14.0-dev — 단말 정밀 조회 및 RF Neighbor 분석 기반. 운영 배포 금지.**
실제 장비 접속, 실측 Airtime, 72시간 구동, Windows Service, HTTPS 및 Aruba 개인 계정 인증은 아직 검증/완료되지 않았습니다. 모의 화면은 실제 측정값이 아닙니다.

## 실행 (개발 환경)
```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python launcher.py --demo
```
브라우저에서 http://127.0.0.1:8765 를 확인합니다. 명시적 --demo 옵션이 있어야 모의 화면이 실행됩니다. 루프백 전용입니다.

## 모의 수집 엔진 화면 (네트워크 접속 없음)

`python launcher.py --engine-demo` 또는 `AWQM.exe --engine-demo` 로 실행합니다. 가상 WLC 4대의 CLI 파서, 수집 Worker, SQLite 저장, 자동/수동 작업 큐를 실제 코드 경로로 연동합니다. 최초 관측 이후 자동 조사는 지속 위험 조건을 충족할 때만 접수합니다. 로컬 임시 DB만 사용하며 종료 시 삭제됩니다. 정밀 분석 완료 후 BSSID/SSID/앵커와 연속 관측이 확인된 단말의 상세 품질을 표시합니다. 실측 Airtime 미검증이며 Station Stats 재전송 비율은 별도 구간으로 표시합니다. AP 상세에서 저장 이력 조회와 HTML/CSV 내보내기를 지원합니다.

## 로컬 HTTPS 인증 검증

`AWQM.exe --auth-preview --help`로 옵션을 확인합니다. 인증 검증은 승인된 개인 계정, 고정 WLC 관리 IP, 검증된 known_hosts와 신뢰 가능한 TLS 인증서, 기존 ID의 역할 매핑이 준비된 경우에만 운영자가 명시적으로 실행합니다. 실제 로그인 검증 경로만 사용하고 대시보드는 합성 데이터입니다. 루프백 전용이며 실제 운영 모드는 아직 제공하지 않습니다. [보안 검증 안내](docs/SECURITY_PREVIEW.md).

모의 수집 모드는 일별 집계 및 중요 DB/집계 백업 스케줄을 실행합니다. 원본 자동 삭제는 아직 보류됩니다. 백업 복구는 새 디렉터리만 허용하며 전체 운영 복구를 의미하지 않습니다.

## 오프라인 CLI 파서 확인
```sh
python launcher.py --validate-cli radio local-output.txt
```
Windows EXE에서도 동일하게 `AWQM.exe --validate-cli radio local-output.txt`를 사용할 수 있습니다. 명령 종류: radio, inventory, active, association, cpu, client, stats, bss. 기본 결과는 건수 또는 CPU 값이며 SSH에 연결하지 않습니다. 실제 CLI 결과는 사내 PC 밖으로 전송하지 않습니다. --detail 출력에는 식별 정보가 포함될 수 있으므로 주의하십시오.

## 승인된 현장 검증용 1회 진단 (선택)

`AWQM.exe --collect-once --help`로 옵션을 확인합니다. 실제 접속은 운영자가 승인된 관리 IP와 검증된 known_hosts, 조회 계정을 제공하고 명시적으로 실행할 때만 수행합니다. 기존 승인된 운영 절차에 따르십시오. 진단 모드는 웹 서버를 열지 않으며, 실제 CLI/장비 호환성은 아직 미검증입니다. 자세한 절차는 [현장 진단 안내](docs/DIAGNOSTIC.md) 참고.

## Windows 진단 서비스 (현장 검증 후보 기능)

빌드 ZIP에 AWQM_Service.exe 및 AWQM_Manager.exe를 추가합니다. 실제 사용은 사내 승인, 비관리자 전용 서비스 계정과 보호된 NTFS 폴더, 검증된 WLC Host Key 및 DPAPI 자격 증명 준비 이후에만 가능합니다. 운영용 RF 분석이나 실데이터 웹 공유는 아직 활성화하지 않습니다. [서비스 구성 및 검증 절차](docs/WINDOWS_SERVICE.md).

## 진단 서비스의 HTTPS 웹 조회 (선택)

기본적으로 웹은 비활성화됩니다. 운영자가 사내 승인된 IP/CIDR, 등록된 인증 WLC, 인증서 SAN 및 기존 개인 ID 권한을 보호된 config/web.json에 명시한 경우에만 서비스의 진단 조회 화면을 활성화합니다. Radio 필드 의미와 호환성은 아직 미검증이며 사용자 식별·자동 원인 판정은 제공하지 않습니다. [진단 웹 설정 및 제한](docs/DIAGNOSTIC_WEB.md).

## 현장 검증 기반 Radio 품질 점수 (선택)

기본값은 미검증 진단 관측입니다. 운영자가 실제 장비의 측정값 의미와 식별 관계를 검증하고 보호된 로컬 검증 기록을 구성하면, 승인된 Radio에 대해서만 기존 균형형 점수/지속 위험 정책을 적용합니다. Host Key·OS·검증 기간이 일치하지 않으면 점수를 제공하지 않습니다. [현장 검증 및 활성화 조건](docs/FIELD_VALIDATION.md).

## Radio 위험 이력 및 운영자 검증

승인된 Radio 통계의 지속 위험, 회복 관측, 확인 불가 및 관측 조건 변경 이력을 보존합니다. RF04(혼잡 관측)과 원인 미확인을 구분하며, 특정 단말이나 외부 간섭 원인은 확정하지 않습니다. 인증된 운영자는 근거 버전을 선택하여 검증 결과를 기록할 수 있습니다. [장애 관측·근거 검증 안내](docs/INCIDENTS.md).

## 현장 승인 기반 단말 정밀 조회 (기본 비활성)

별도 client-validation.json으로 Radio 검증 및 Client Table 지표 의미·개인정보 처리 승인을 연결해야 합니다. 지속적인 Radio 혼잡 AP만 자동 정밀 조회 대상으로 선정합니다. 임시 식별자 기반 단말 품질 관측을 제공하며 MAC·SSID 원문은 단말 이력 DB에 저장하지 않습니다. 재전송·Airtime·실제 원인 기여는 아직 산정하지 않습니다. [단말 상세 수집 안내](docs/CLIENT_VALIDATION.md).

## RF Neighbor 오프라인 검증

`AWQM.exe --validate-cli neighbors local-arm-neighbors.txt`로 AOS 8.9+ 형식의 저장된 ARM Neighbor 결과를 로컬에서 검사합니다. 채널 블록 중첩과 상관관계는 명시적으로 검증된 채널 구성과 직접 관측을 요구합니다. 현재 실제 Neighbor 자동 수집 및 장애 후보 통합은 활성화하지 않습니다. [RF 분석 계약](docs/RF_NEIGHBORS.md).

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
