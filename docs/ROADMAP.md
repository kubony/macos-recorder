# macOS Recorder 미팅 자동화 로드맵

작성일: 2026-02-22

---

## 비전

macOS Recorder로 수집한 데이터(화면, 카메라, 마이크, 시스템오디오, BLE)를 활용하여 회의 전반을 자동화한다.

- BLE 디바이스-사람 매핑을 반복 회의를 통해 점진적으로 학습
- 회의 장소, 시간, 참석자, 내용을 자동으로 인식
- 미팅 노트를 자동 생성하여 지식 자산으로 축적

---

## 아키텍처: 3개 레이어

### 1. Data Collection Layer (기존 macOS Recorder 확장)

현재 macOS Recorder가 수집하는 데이터를 확장하는 레이어.

- 마이크 오디오 (`mic.wav`) - 내 목소리
- 시스템 오디오 (`audio.wav`) - 상대방 목소리
- BLE 스캔 이벤트 (`events.jsonl`) - 주변 기기 신호
- 화면 캡처, 카메라 영상 (선택적)

### 2. Device Registry (`~/.macos-recorder/device_registry.json`)

BLE 디바이스와 사람을 연결하는 로컬 지식 베이스.

- 디바이스 주소 → 사람 매핑
- 장소 fingerprint (고정 기기 조합으로 장소 판정)
- 신뢰도 점수 (반복 관찰로 Bayesian 업데이트)

### 3. Meeting Pipeline

수집된 원시 데이터를 구조화된 미팅 노트로 변환하는 파이프라인.

- STT: `mic.wav` → `transcript.txt`
- 요약: `transcript.txt` → Claude API → `notes.md`
- 인덱싱: 미팅별 메타데이터를 `meetings_index.jsonl`에 누적

---

## 핵심 설계 결정

### Audio is King

마이크 오디오가 전체 가치의 80%를 담당한다.

- `mic.wav` → Whisper → Claude → `notes.md` 파이프라인이 핵심
- 화면 OCR, 실시간 처리 등 복잡한 기능보다 이 파이프라인 완성도가 우선

### BLE는 보조 신호

BLE는 ML 없이 단순 dictionary lookup으로 활용한다.

- `known_devices.json` 파일에 주소 → 이름 매핑만 저장
- 머신러닝 모델 학습 불필요
- 신호가 없어도 미팅 노트 생성에는 영향 없음

### 화자 분리 전략

하드웨어 레벨에서 물리적으로 분리한다.

- `mic.wav` = 내 목소리 (마이크 입력)
- `audio.wav` = 상대방 목소리 (시스템 오디오)
- 별도 화자 분리 모델 불필요

### Fine-tuning 안 함

커스텀 모델 학습은 하지 않는다.

- 프롬프트 엔지니어링 + RAG로 충분
- 유지보수 비용 대비 실익 없음

### 스토리지 전략

JSON 파일로 충분하다. SQLite 불필요.

- 연간 수백 개 규모의 미팅 → JSON 파일로 처리 가능
- `meetings_index.jsonl` + 개별 `notes.md` 파일 구조

### 장소 감지 방식

고정 BLE 기기 조합으로 장소를 자동 판정한다.

- TV, 가전제품 등 이동하지 않는 기기의 BLE 신호를 fingerprint로 활용
- 알려진 fingerprint와 일치하면 장소 라벨 자동 부여

---

## 구현 로드맵

### 즉시 (현재)

BLE 데이터 수집 확장.

- `events.jsonl`에 기록하는 필드 확장
- 기존 `name`, `rssi` 외에 `address`, `tx_power`, `manufacturer_data`, `service_uuids`, `service_data` 추가

### Week 1

STT 파이프라인 구축.

- `mic.wav` → Whisper API 또는 로컬 Whisper → `transcript.txt`
- 녹음 완료 후 자동 실행 스크립트 작성

### Week 2

미팅 노트 자동 생성.

- `transcript.txt` → Claude API → `notes.md`
- 미팅 메타데이터를 `meetings_index.jsonl`에 추가
- 기본 프롬프트 템플릿 작성 (결정사항, 액션아이템, 요약 포함)

### Week 3

BLE 참석자 매핑 연동.

- `known_devices.json`에 디바이스 주소 → 사람 이름 수동 등록
- 미팅 시간대 BLE 스캔 결과와 대조하여 참석자 자동 기록
- 노트에 참석자 필드 자동 삽입

### Month 2 이후

누적 데이터 활용.

- RAG 검색: 과거 미팅 노트에서 관련 내용 검색
- 동시출현 분석: 자주 함께 등장하는 기기 → 같은 사람 소유 추정
- 장소 fingerprint 자동 학습

---

## 만들지 않는 것

아래 기능은 복잡도 대비 실익이 없어 구현하지 않는다.

- 실시간 STT (녹음 완료 후 처리로 충분)
- 화면 OCR (텍스트 추출 가치 낮음)
- 커스텀 대시보드 (파일 시스템 + 텍스트 검색으로 충분)
- BLE 실내 측위 (RSSI 기반 위치 추정은 정확도가 낮음)
- 자동 녹화 시작 (수동 시작이 더 안전)
- Fine-tuning (프롬프트 + RAG로 대체)

---

## BLE 데이터 스키마 (목표)

`events.jsonl`의 각 레코드에 포함할 확장 필드.

- `address`: CoreBluetooth UUID (기기 고유 식별자)
- `name`: 기기 이름 (있을 경우)
- `rssi`: 수신 신호 강도 (dBm)
- `tx_power`: 송신 전력 (광고 패킷에 포함된 경우)
- `manufacturer_data`: 제조사 데이터 (`{company_id: hex_bytes}` 형식)
- `service_uuids`: 광고하는 서비스 UUID 목록
- `service_data`: 서비스별 데이터 (`{uuid: hex_bytes}` 형식)

---

## Device Registry 스키마

`~/.macos-recorder/device_registry.json` 구조.

- `devices`: 디바이스 주소 → 정보 매핑
  - 각 항목: `person`, `confidence`, `device_type`, `known_names`, `first_seen`, `last_seen`, `meeting_count`
- `people`: 사람 이름 → 정보 매핑
  - 각 항목: `devices` (주소 목록), `primary_device`, `role`, `email`
- `infrastructure`: 고정 기기 주소 목록 (TV, 가전 등 장소 fingerprint용)
- `locations`: 장소 ID → 정보 매핑
  - 각 항목: `fingerprint_devices` (고정 기기 목록), `label` (장소 이름)

---

## 통계적 접근 (Month 2 이후)

### 기기 클러스터링 (같은 사람 소유 기기 묶기)

- 이름 패턴 유사도 + RSSI 시계열 Spearman 상관계수 (rho > 0.6이면 동일인 가능성 높음)

### 참석자 존재 판정 기준

아래 조건을 모두 충족하면 참석자로 인정.

- 체류 시간 2분 초과
- 중간값 RSSI > -70 dBm
- 미팅 시간 내 탐지 비율 > 60%

### 미팅 경계 감지

고유 기기 수의 급격한 변화점(change point)을 기준으로 미팅 시작/종료 자동 감지.

### 신뢰도 업데이트

Bayesian 방식으로 관찰 횟수에 따라 신뢰도 누적.

- 이름이 있는 Apple 기기: 3번 관찰 후 95% 신뢰도 도달
- 이름 없는 기기: 더 많은 관찰 필요

---

## 복리 효과 (Compound Effect)

미팅 데이터가 쌓일수록 활용 가치가 기하급수적으로 증가한다.

- 10회: 핵심 결정사항 검색 가능
- 50회: 반복 패턴 및 주요 주제 가시화
- 100회: 사람별 커뮤니케이션 패턴 분석 가능
- 500회: 6개월 프로젝트 히스토리 전체 검색 가능

---

## 관련 파일 구조

```
~/.macos-recorder/
├── device_registry.json     # BLE 디바이스 ↔ 사람 매핑
└── known_devices.json       # 간단한 주소 → 이름 룩업 테이블

~/Documents/녹음아카이브/    # 또는 각 미팅별 디렉토리
├── 2026-02-22_meeting/
│   ├── mic.wav
│   ├── audio.wav
│   ├── events.jsonl
│   ├── transcript.txt
│   └── notes.md
└── meetings_index.jsonl     # 전체 미팅 인덱스
```
