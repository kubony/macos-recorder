---
title: BLE 오버레이 + Settings UI
date: 2026-03-02
status: approved
---

# BLE 오버레이 + Settings UI

## Goal

macos-recorder에 BLE 인물 오버레이 + Settings UI 추가

## Feature 1: BLE 오버레이

- 화면 우상단에 반투명 플로팅 패널
- 녹화와 독립적으로 ON/OFF 가능
- 매핑된 기기: "유태준 (iPhone, -51dBm)" 형태
- 미매핑 기기: 표시/숨김 설정 가능
- RSSI 임계값: 설정에서 조절 가능 (기본 -70 dBm)
- 매핑 소스: `ble_identify.py` 전용 레지스트리만 사용 (인물사전 X)

## Feature 2: Settings UI

- macOS 환경설정 스타일 NSWindow (탭 구분)
- 메뉴바에서 "Settings..." 클릭으로 열기
- 기존 config.yaml 직접 수정 → GUI로 대체
- 탭 구성: General / Audio / BLE & Overlay / Privacy
- UI 변경 → config.yaml 자동 저장

## Constraints

- 기존 AppKit 패턴 유지 (label_panel.py 참고)
- config.yaml 연동 (UI 변경 → YAML 저장)
- RSSI 기본값 -70 dBm

## Settings 항목 (BLE & Overlay 탭)

- BLE 스캔 ON/OFF
- BLE 스캔 간격
- BLE 익명화 ON/OFF
- 오버레이 ON/OFF (녹화와 독립)
- 오버레이 위치: 우상단 고정
- 미매핑 기기 표시/숨김
- RSSI 필터링 임계값 (슬라이더)

## Success Criteria

- 녹화 없이도 오버레이 ON → BLE 기기 실시간 표시
- Settings 창에서 오버레이/BLE 옵션 변경 → 즉시 반영
- 미매핑 기기 표시/숨김 토글 동작
- RSSI 슬라이더로 필터링 범위 조절

## Technical Notes

- 기존 코드: rumps(메뉴바) + AppKit(NSPanel) + bleak(BLE) + config.yaml
- ble_identify.py에 이미 BLE 기기-인물 매핑 레지스트리 존재
- label_panel.py의 NSPanel 패턴 참고하여 오버레이 구현
- BluetoothMonitor(recorder.py)가 이미 BLE 스캔 수행 중
