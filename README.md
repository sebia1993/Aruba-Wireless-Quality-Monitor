# Aruba Wireless Quality Monitor (AWQM)

Windows 11 / ArubaOS 8 클러스터 기반 무선 품질 분석 도구.

**현재 단계: v0.40.0-dev — 브라우저 로그인·로그아웃 호환성 수정 및 보안 회귀 검증. 운영 배포 금지.**
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

## 별도 Station Stats 현장 검증 (기본 비활성)

현장 승인된 단말 상세 수집에 한해 Station Stats의 성공 프레임 재시도 구간 분석을 별도 승인으로 활성화할 수 있습니다. 동일 접속 조건과 최소 프레임 수가 충족되지 않으면 미확인으로 표시합니다. Airtime 실측은 아직 활성화하지 않습니다. [Station Stats 검증 안내](docs/STATION_VALIDATION.md).

## WLC 클러스터 상태 및 부하 관측

WLC별 CPU·Peer 상태 및 Active/Standby AP·단말 수를 종합 현황에서 확인할 수 있습니다. 클러스터 전체 부하는 한 대표 출처로 계산하고, 동일 관측을 중복 합산하지 않습니다. 누락 및 상태 상충은 미확인으로 처리하며 장비 Hang/Down을 확정하지 않습니다. [클러스터 관측 안내](docs/CLUSTER.md).

## 장애 보고서 및 데이터 보존

위험 관측 상세에서 HTML·CSV 보고서와 주간보고 요약을 제공합니다. 실제 서비스 영향이나 조치 결과를 임의 생성하지 않습니다. 선택적 로컬 보존 정책은 집계와 백업, 장애 근거가 검증된 경우만 오래된 원본을 삭제합니다. 기본값은 자동 삭제 비활성입니다. [유지보수 안내](docs/MAINTENANCE.md).

## 선택적 RF Neighbor 실제 조회

별도 현장 검증 기록을 통해 BSSID 지정 조회와 수신 Radio·채널 블록·OTA 시간을 검증한 경우에만 자동 RF Neighbor 조회가 가능하도록 연결했습니다. 실제 원인 확정이나 물리 위치 추정은 하지 않습니다. [RF Neighbor 검증 조건](docs/RF_NEIGHBORS.md).

## 선택적 단말 접속 관측 이력

현장 검증 및 개인정보/보존 승인, 별도 DPAPI 키가 있는 경우 관리자 전용 30일 단말 접속 관측 이력과 A/S-UAC 테이블 관측을 제공합니다. Radio와 분리된 association.db를 사용하며 원본 단말 식별 정보를 저장하지 않습니다. [설정 및 제약](docs/ASSOCIATION_HISTORY.md).

## 규모 검증 및 업데이트 준비

[검증 안내](docs/VALIDATION.md)와 [오프라인 업데이트 준비](docs/PACKAGE_STAGE.md). 전체 합성 규모의 단회 수집 검증은 개발 측에서 수행합니다. 72시간 연속 구동 시험은 사용자 직접 검증 항목(2026-11-25 확정)으로 분리하며, 실제 시험 결과 확인 전에는 통과로 기록하지 않습니다. 패키지 스테이징은 설치나 서비스 변경을 수행하지 않습니다.

## 선택적 수동 정밀 조사 예약

현장 승인된 지속 혼잡 AP를 선택해 Client Table 조회를 예약하고, 동일 요청 재전송 및 자동 작업 중복 접수를 방지합니다. 실행 전 물리 AP 식별과 보호 정책을 다시 확인합니다. [수동 조사 및 제한](docs/MANUAL_JOBS.md).

## Radio 관측 이력과 추세

AP 상세에서 원본 7일, 5분 집계 30일, 1시간 집계 365일 조회 및 HTML/CSV 출력과 채널 추세 차트를 지원합니다. 미집계·결측 구간을 정상으로 간주하지 않습니다. [장기 이력 안내](docs/DIAGNOSTIC_HISTORY.md).

## 점검 시간 수집 제어

관리자 전용 WLC별/전체 수집 일시정지·재개와 로컬 관리자 복구 기능을 제공합니다. 요청 접수와 실제 적용을 구분하고 진행 중인 조회는 종료할 때까지 기다립니다. 장비 설정이나 재부팅은 수행하지 않습니다. [점검 제어 안내](docs/OPERATIONS.md).

## 업데이트 준비 및 롤백 복구 사본

정지된 서비스의 이전 실행 파일, 보호된 설정·자격 증명·DB·로그와 신규 ZIP을 검증된 복구 묶음으로 생성합니다. 기존 설치를 덮어쓰거나 서비스를 시작하지 않습니다. [업데이트 준비 절차](docs/UPDATE_SNAPSHOT.md).

## 중지된 서비스의 프로그램 교체

승인된 ZIP과 기존 복구 묶음을 검증한 후 중지·시작 비활성화 상태에서 프로그램 폴더만 교체하고, 운영 상태 변경 전 이전 프로그램으로 되돌리는 기능입니다. 실제 설치 검증은 아직 필요하며 운영 데이터 자동 덮어쓰기 및 자동 시작은 제공하지 않습니다. [프로그램 교체 및 제한](docs/PROGRAM_SWITCH.md).

## 실시간 화면 알림

인증된 WSS로 새 게시 상태를 알리고 기존 HTTPS 조회로 갱신합니다. 연결 불가 시 주기 조회로 대체하며 장비 명령은 전송하지 않습니다. [실시간 알림 안내](docs/REALTIME.md).

## 선택적 단말 재시도 상관관계

별도 현장 승인과 개인정보 검증 후 동일 Radio의 시간 정렬된 단말 품질 저하를 비교하여 RF03 간접 후보를 표시합니다. 단말 원인 확정 및 실측 Airtime은 제공하지 않습니다. [비교 기준 및 제한](docs/CLIENT_CORRELATION.md).

## 선택적 RF03 집계 근거 보존

별도 보존 승인 시 Radio 수준의 비교·저하 단말 수 및 관측 시각만 장애 이력에 기록합니다. 개인 단말 식별자 또는 원인 확정은 저장하지 않습니다. [승인 및 제한](docs/CLIENT_CORRELATION.md).

## HTTPS·WSS 동시 조회 검증

가상 사용자 10개 연결, 50회 HTTPS 조회와 알림 전달을 실제 루프백 TLS 연결로 시험하는 `--validate-scale https` 검증 모드를 추가했습니다. 실제 브라우저·AAA·장비 검증과는 별개입니다. [검증 안내](docs/VALIDATION.md).

## 장기 보존 DB 검증

`--validate-scale retained`로 32 Radio의 365일 보존 데이터 조회를 검증합니다. 별도 `retained-full` 모드는 4,000 Radio, 109,920,000행 합성 DB를 생성하므로 전용 테스트 저장소에서만 실행합니다. 실제 수집·집계·72시간 검증과 구분합니다. [검증 안내](docs/VALIDATION.md).

## 선택적 WLC 메모리·Datapath 관측

별도 현장 검증과 승인 후 메모리 요약과 Datapath CPU별 1초·4초 사용률을 조회합니다. CPU 목록·검증 만료·보호 상태를 확인하며 64초 통계는 원인 판단에 사용하지 않습니다. [자원 관측 안내](docs/CONTROLLER_RESOURCES.md).

## 빌드 의존성 보안 검증

Windows ZIP의 실제 설치 패키지 버전을 별도 CI에서 취약점 조회하고 누락·검사 실패·알려진 취약점은 보안 검증 미통과로 처리합니다. [보안 검증 범위](docs/DEPENDENCY_AUDIT.md).

## SSH 의존성 업데이트 및 로컬 검증

`--validate-ssh`로 실제 루프백 SSH의 Host Key 확인·RSA/SHA-2·인증 및 CLI 동작을 확인합니다. Paramiko 5.1.0·cryptography 50.0.2로 버전을 고정하고 이전 취약 버전으로 자동 전환하지 않습니다. 구형 SSH 알고리즘만 지원하는 실제 장비는 현장 호환성 확인이 필요합니다. [보안 검증 안내](docs/DEPENDENCY_AUDIT.md).

## AP Standby / UAC DB 관측

별도 현장 검증 후 AP Standby 및 UAC AP DB의 현재 물리 AP 일치 관측을 제공하며, 단말 UAC 또는 장애 상태로 단정하지 않습니다. [검증 및 제한](docs/AP_ANCHORS.md).

## HTTPS 요청 보호

절대 연결 시간과 헤더 크기를 제한하고 정상 WSS 전환을 보존합니다. [보안 제한](docs/HTTP_BOUNDS.md).

## 업데이트 이후 데이터 복구

설정과 자격 증명이 이전 사본과 동일한 중지·시작 비활성 서비스에 한해, 신규 데이터와 로그를 별도 보존하고 이전 데이터 및 프로그램을 복원합니다. 자동 시작하지 않습니다. [복구 조건 및 제한](docs/DATA_ROLLBACK.md).

## Airtime 현장 검증용 오프라인 검사

`--validate-cli client-info-airtime`와 `--compare-file`로 두 출력의 TID 필드 구조·변화 수를 확인합니다. 실제 Airtime 수치나 원인 확정은 하지 않습니다. [검증 기준](docs/AIRTIME_VALIDATION.md).

## SQLite 저장 및 복구 검증

`--validate-scale lifecycle --output <새 폴더>`로 실제 저장 경로의 합성 데이터 재처리, 일별 집계, 백업/복원, 명시적 원본 보존 정책을 검증합니다. 32 Radio/120개 시점의 대표 규모이며 전체 용량 시험은 별도입니다. [검증 안내](docs/VALIDATION.md).

## 진단 웹 화면 검증

별도 Chromium CI에서 실제 로그인, AP 검색/페이지 이동, 상세 및 이력 내보내기, 메뉴 전환, 권한과 세션 만료, WSS 및 두 화면 크기를 합성 데이터로 검사합니다. 브라우저 테스트 도구는 EXE에 포함하지 않습니다. [검증 안내](docs/VALIDATION.md).

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
