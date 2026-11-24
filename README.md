# Aruba Wireless Quality Monitor (AWQM)

Windows 11 / ArubaOS 8 클러스터 기반 무선 품질 분석 도구.

**현재 단계: v0.1.0-dev — 개발 기반 및 모의 대시보드. 운영 배포 금지.**
실제 장비 접속, 실측 Airtime, 72시간 구동, Windows Service, HTTPS 및 Aruba 개인 계정 인증은 아직 검증/완료되지 않았습니다. 모의 화면은 실제 측정값이 아닙니다.

## 실행 (개발 환경)
```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python launcher.py --demo
```

브라우저에서 http://127.0.0.1:8765 를 확인합니다. 첫 버전은 명시적인 --demo 옵션이 있어야 실행되며 로컬 루프백으로만 접근 가능합니다. 실제 장비 수집/원격 접속은 지원하지 않습니다.

## Windows 빌드
```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --name AWQM --add-data "awqm/static;awqm/static" launcher.py
dist\AWQM\AWQM.exe --demo
```
GitHub Actions의 Windows ZIP artifact는 개발용입니다. 정식 릴리스로 간주하지 않습니다.

- [확정 설계 기준 및 개발 범위](docs/DESIGN.md)
- [작업 현황 및 검증 게이트](docs/STATUS.md)

실제 계정, AP 이름, IP, SSID, CLI 원본, DB 및 인증서를 공개 저장소에 올리지 않습니다.
