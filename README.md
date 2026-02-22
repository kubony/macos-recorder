# macOS Recorder

High-performance desktop recorder for macOS. Captures screen, audio, microphone, and Bluetooth signal strength.

Inspired by [ocap](https://github.com/open-world-agents/ocap) from the D2E project.

## Features

- 🖥️ **Screen Recording**: Capture screen with hardware acceleration
- 🔊 **System Audio**: Record system audio output
- 🎤 **Microphone**: Record microphone input
- 📶 **Bluetooth RSSI**: Log Bluetooth device signal strength
- 😴 **Sleep Prevention**: Prevents Mac from sleeping during recording
- 🔴 **Status Indicator**: Menu bar icon shows recording status
- ⏱️ **Synchronized Timestamps**: All data streams aligned

## Requirements

- macOS 12.0+ (Monterey or later)
- Python 3.10+
- Screen Recording permission
- Microphone permission
- Bluetooth permission

## Installation

```bash
# Clone the repository
git clone https://github.com/kubony/macos-recorder.git
cd macos-recorder

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

### GUI Mode (Menu Bar App)

```bash
python main.py
```

Click the menu bar icon (🔴) to:
- Start/Stop recording
- View recording status
- Access settings
- Quit application

### CLI Mode

```bash
# Start recording (stop with Ctrl+C)
python recorder.py output_recording

# With options
python recorder.py output --fps 30 --no-audio --no-bluetooth
```

### Output Files

- `{name}_screen.mp4` — Screen recording with audio
- `{name}_events.json` — Timestamped events (keyboard, mouse, bluetooth)
- `{name}_mic.wav` — Microphone recording (separate track)

## Configuration

Edit `config.yaml` or use environment variables:

```yaml
recording:
  fps: 30
  quality: high  # low, medium, high
  include_cursor: true
  
audio:
  system_audio: true
  microphone: true
  sample_rate: 44100
  
bluetooth:
  enabled: true
  scan_interval: 1.0  # seconds
  target_devices: []  # empty = all devices
  
output:
  directory: ~/Recordings
  format: mp4
```

## Permissions Setup

On first run, macOS will request permissions:

1. **Screen Recording**: System Preferences → Privacy & Security → Screen Recording
2. **Microphone**: System Preferences → Privacy & Security → Microphone
3. **Bluetooth**: System Preferences → Privacy & Security → Bluetooth

## Architecture

```
macOS Recorder
├── Screen Capture (ScreenCaptureKit / AVFoundation)
├── Audio Capture (AVFoundation)
│   ├── System Audio (requires BlackHole or similar)
│   └── Microphone
├── Bluetooth Monitor (CoreBluetooth)
├── Sleep Inhibitor (IOKit)
└── Menu Bar UI (rumps)
```

## Data Format

Events are logged in JSON Lines format with nanosecond timestamps:

```json
{"ts": 1708588800000000000, "type": "bluetooth", "device": "AirPods Pro", "rssi": -45}
{"ts": 1708588801000000000, "type": "bluetooth", "device": "AirPods Pro", "rssi": -47}
```

## License

MIT License

## Acknowledgments

- [ocap](https://github.com/open-world-agents/ocap) - Original Windows implementation
- [D2E](https://worv-ai.github.io/d2e/) - Research project that inspired this tool
- [rumps](https://github.com/jaredks/rumps) - Menu bar app framework
