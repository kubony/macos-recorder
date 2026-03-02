# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

macOS menu bar 녹화 앱. 화면(ffmpeg+avfoundation), 시스템 오디오(BlackHole+sounddevice), 마이크(sounddevice), 카메라(ffmpeg), BLE RSSI(bleak)를 동시 녹화하고 나노초 타임스탬프로 동기화한다.

## Two Implementations

- **Full version**: `main.py` + `recorder.py` — 프로덕션용. rumps 메뉴바 앱, crash recovery, GDPR consent, event buffering, thread safety
- **Minimal version**: `main_minimal.py` — 단일 파일 213줄. ffmpeg 직접 호출, bleak 직접 사용. `setup.py`의 py2app 빌드 타겟

## Commands

```bash
# 실행 (GUI)
python main.py              # Full version
python main_minimal.py      # Minimal version

# 실행 (CLI)
python recorder.py output_name --fps 30 --no-audio --no-bluetooth

# 테스트
pytest tests/ -v

# 린트/포맷
black .
isort .
mypy recorder.py main.py

# macOS .app 빌드
python setup.py py2app       # dist/macOS Recorder.app 생성
```

## System Dependencies

- `brew install ffmpeg` — 화면/카메라 녹화 필수
- `brew install blackhole-2ch` — 시스템 오디오 캡처 필수. Audio MIDI Setup에서 Multi-Output Device 설정 필요

## Architecture

```
main.py (RecorderApp - rumps.App)
  ├── recorder.py::ScreenRecorder    — ffmpeg avfoundation h264_videotoolbox
  ├── recorder.py::AudioRecorder     — sounddevice + soundfile streaming (system/mic)
  ├── recorder.py::BluetoothMonitor  — bleak BleakScanner, 별도 asyncio loop on daemon thread
  ├── recorder.py::SleepInhibitor    — NSProcessInfo + caffeinate subprocess
  ├── recorder.py::BluetoothAnonymizer — SHA-256 LRU cache (5000 device limit)
  └── ConsentManager                 — GDPR consent to ~/.macos-recorder/consent.json
```

## Recording Session Output

```
~/Recordings/recording_YYYYMMDD_HHMMSS/
├── screen.mp4           # h264_videotoolbox hardware encoding
├── camera.mp4           # 카메라 녹화 (minimal version)
├── audio.wav            # BlackHole system audio (PCM_16 44100Hz stereo)
├── mic.wav              # 마이크
├── events.jsonl         # BLE RSSI + recording start/stop events
├── reference_time.json  # wall + monotonic timestamp (A/V sync 기준)
└── COMPLETE/INCOMPLETE  # completion marker
```

## Key Patterns

- **ffmpeg 4-stage shutdown**: stdin `q` → 10s wait → SIGTERM → SIGKILL
- **Event buffering**: 100개 or 1초 간격 flush to events.jsonl, POSIX atomic write
- **Config**: `config.yaml` deep-merge over hardcoded defaults in `load_config()`
- **Thread safety**: `threading.Event` for recording flag, `threading.Lock` for event buffer
- **Version**: `version.py`가 single source of truth (`__version__ = "1.1.0"`)

## Config

`config.yaml` 편집. 모든 설정은 `recorder.py::load_config()`에 하드코딩된 기본값이 있어 누락되어도 동작.
