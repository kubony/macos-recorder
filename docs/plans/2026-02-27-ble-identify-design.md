# BLE 참석자 식별 도구 설계

작성일: 2026-02-27

## 목적

녹음 세션의 BLE 데이터를 분석하여 미팅 참석자를 자동 추론하고, 사용자가 확인/수정할 수 있는 CLI 도구. 확정된 매핑은 자동 학습되어 다음 분석의 정확도를 높인다.

## 데이터 모델

### Device Registry (`~/.macos-recorder/device_registry.json`)

기존 `ble_device_owners.json`을 마이그레이션한 장치-사람 매핑 DB.

- `devices`: 장치 이름 → 정보 매핑
  - `owner`: 소유자 이름
  - `confidence`: certain / high / medium / low
  - `aliases`: 동일 장치의 다른 이름 목록 (예: Galaxy Buds 교대 광고 ID)
  - `first_seen`: 최초 관찰 날짜
  - `session_count`: 확인된 세션 수

- `people`: 사람 이름 → 정보 매핑
  - `affiliation`: 소속
  - `devices`: 소유 장치 이름 목록

- `ambient`: 사무실 고정 장치 이름 목록 (무시 대상)

### participants.json (세션별)

기존 형식 유지, 필드 추가:

- `source`: `"ble"` (자동 감지), `"manual"` (사용자 추가), `"confirmed"` (BLE 추론 후 사용자 확인)
- `ble_devices`: 감지된 장치 목록 (source가 ble/confirmed일 때)

## CLI 인터페이스

```
ble-id analyze <session_path> [--yes] [--json]
ble-id list <session_path>
ble-id add <session_path> <name> [--affiliation <소속>]
ble-id remove <session_path> <name>
ble-id edit <session_path> <name> [--name <새이름>] [--affiliation <소속>]
ble-id registry
ble-id migrate
```

## 분석 알고리즘

1. events.jsonl에서 `type: "bluetooth"` 이벤트 추출
2. 장치별 그룹핑 (name 기준, "Unknown" 제외)
3. 참석 판정 기준 적용:
   - 중간값 RSSI > -70 dBm
   - 미팅 시간 내 탐지 비율 > 60%
   - 체류 시간 2분 초과
4. device_registry 기반 known 장치 매칭
5. ambient 장치 필터링
6. 미지 장치 후보 추출

## 인터랙티브 확인 흐름

1. 확정 참석자 표시 (registry 매칭)
2. 미확인 장치별 확인 프롬프트 (Y/n/다른이름)
3. 추가 참석자 수동 입력
4. participants.json 저장
5. device_registry 자동 학습

## 자동 학습

- 확인 시: registry의 session_count 증가, confidence 상승
- 수동 추가 시: 해당 세션의 미매핑 장치를 후보로 기록
- Confidence 등급: certain (5+회) → high (3~4회) → medium (1~2회) → low (미확인)

## 파일 위치

- `ble_identify.py` — macos-recorder 프로젝트 루트
- `~/.macos-recorder/device_registry.json` — 장치 매핑 DB
- 각 세션의 `participants.json` — 세션 디렉토리 내

## 의존성

표준 라이브러리만 사용 (json, argparse, statistics, pathlib). 외부 의존성 없음.
