#!/usr/bin/env python3
"""
macOS Recorder - Menu bar app for recording screen, audio, mic, and Bluetooth RSSI.
Inspired by ocap from the D2E project.

Improvements based on expert review:
- Permission checking and onboarding
- Config file loading
- Recording options toggle
- Status feedback (icon animation, sounds, duration)
- Consent mechanism
- Secure file handling
- P0 fixes: thread safety, event buffering, crash recovery, size update optimization
"""

import rumps
import threading
import time
import os
import json
import subprocess
import signal
import atexit
import logging
from datetime import datetime
from pathlib import Path

from recorder import (
    ScreenRecorder, CameraRecorder, AudioRecorder, BluetoothMonitor, ContextMonitor,
    LocationMonitor, SleepInhibitor, AudioOutputSwitcher, PermissionChecker, load_config,
    secure_directory, secure_file, play_sound, find_all_screens
)
from label_panel import LabelPanel
from overlay_panel import BLEOverlayPanel
from settings_panel import SettingsPanel
from version import __version__

# Setup logging
logger = logging.getLogger(__name__)


# P0 fix: State file for crash recovery
STATE_FILE = Path.home() / ".macos-recorder" / "state.json"

# P1 fix (Memory): Event buffer limits
MAX_EVENT_BUFFER = 10000


class ConsentManager:
    """Manages user consent for data collection (GDPR compliance)."""
    
    CONSENT_FILE = Path.home() / ".macos-recorder" / "consent.json"
    CONSENT_VERSION = "1.0"
    
    def __init__(self):
        self.CONSENT_FILE.parent.mkdir(exist_ok=True)
    
    def has_consent(self) -> bool:
        """Check if user has given consent."""
        if not self.CONSENT_FILE.exists():
            return False
        try:
            data = json.loads(self.CONSENT_FILE.read_text())
            return data.get("granted", False) and data.get("version") == self.CONSENT_VERSION
        except (json.JSONDecodeError, KeyError, IOError) as e:
            logger.warning(f"Consent file error: {e}")
            return False
    
    def request_consent(self) -> bool:
        """Show consent dialog and save response."""
        response = rumps.alert(
            title="📹 개인정보 수집 동의",
            message=(
                "macOS Recorder는 다음 정보를 수집합니다:\n\n"
                "• 화면 녹화 (모든 화면 내용)\n"
                "• 시스템 오디오 및 마이크\n"
                "• 블루투스 기기 신호 강도 (익명화됨)\n\n"
                "수집된 데이터는 로컬에만 저장되며,\n"
                "언제든지 삭제할 수 있습니다.\n\n"
                "동의하시겠습니까?"
            ),
            ok="동의",
            cancel="거부"
        )
        
        consent_data = {
            "granted": response == 1,
            "version": self.CONSENT_VERSION,
            "timestamp": datetime.now().isoformat()
        }
        
        self.CONSENT_FILE.write_text(json.dumps(consent_data, indent=2))
        return response == 1


class RecorderApp(rumps.App):
    """Menu bar application for macOS recording."""
    
    def __init__(self):
        super().__init__(
            name="macOS Recorder",
            icon=None,
            title="⚫",
            quit_button=None
        )
        
        # P0 fix: thread-safe recording flag
        self._recording = False
        self._recording_lock = threading.Lock()
        self.start_time = None
        
        # P0 fix: event buffering
        self._event_buffer = []
        self._event_buffer_lock = threading.Lock()
        self._last_size_update = 0  # P0 fix: throttle size updates
        self._cached_size = 0
        
        # Load config
        self.config = load_config()
        self.output_dir = Path(self.config["output"]["directory"]).expanduser()
        self.output_dir.mkdir(exist_ok=True)
        secure_directory(self.output_dir)
        
        # P0 fix: recover from previous crash
        self._recover_from_crash()
        
        # P0 fix: register cleanup handlers
        atexit.register(self._cleanup_on_exit)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        
        # Detect available screens
        self._available_screens = find_all_screens() or [(1, "Default")]

        # Recording settings (toggleable)
        self.settings = {
            "record_screen": True,
            "screen_selection": "all",  # "all" or index like 0, 1, 2
            "record_audio": self.config["audio"]["system_audio"],
            "record_mic": self.config["audio"]["microphone"],
            "record_camera": self.config["recording"].get("camera", True),
            "record_bluetooth": self.config["bluetooth"]["enabled"],
            "record_context": self.config["context"].get("enabled", True),
            "record_gps": self.config.get("gps", {}).get("enabled", False),
            "show_labels": self.config.get("labeling", {}).get("enabled", True),
            "fps": self.config["recording"]["fps"],
            "anonymize_bluetooth": self.config["bluetooth"].get("anonymize", True),
            "overlay_enabled": self.config.get("overlay", {}).get("enabled", False),
        }
        
        # Recording components
        self.screen_recorders = []
        self.camera_recorder = None
        self.audio_recorder = None
        self.mic_recorder = None
        self.bluetooth_monitor = None
        self.context_monitor = None
        self.location_monitor = None
        self.label_panel = None
        self.sleep_inhibitor = None
        self.audio_switcher = None

        # Overlay & settings
        self.overlay_panel = None
        self.settings_panel = None
        self._standalone_ble = None  # BLE monitor for overlay when not recording
        
        # Event log
        self.event_file = None
        self.session_dir = None
        
        # Consent manager
        self.consent_manager = ConsentManager()
        
        # Build menu with toggles
        self._build_menu()
        
        # Timer for updating duration
        self.duration_timer = rumps.Timer(self._update_duration, 1)
        
        # Check permissions and consent on startup
        self._check_startup_requirements()

        # Restore overlay after RunLoop starts (deferred)
        if self.settings["overlay_enabled"]:
            self._startup_overlay_timer = rumps.Timer(self._deferred_show_overlay, 1)
            self._startup_overlay_timer.start()
    
    @property
    def recording(self) -> bool:
        with self._recording_lock:
            return self._recording
    
    @recording.setter
    def recording(self, value: bool):
        with self._recording_lock:
            self._recording = value
    
    def _recover_from_crash(self):
        """Check for incomplete recording from previous crash."""
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
                if state.get("recording") and state.get("session_dir"):
                    session_dir = Path(state["session_dir"])
                    if session_dir.exists():
                        # Mark as incomplete
                        incomplete_marker = session_dir / "INCOMPLETE"
                        incomplete_marker.write_text(f"Crashed at: {datetime.now().isoformat()}")
                        rumps.notification(
                            title="macOS Recorder",
                            subtitle="",
                            message=f"이전 녹화가 비정상 종료됨:\n{session_dir.name}"
                        )
            except:
                pass
            finally:
                STATE_FILE.unlink(missing_ok=True)
    
    def _save_state(self):
        """Save current recording state for crash recovery."""
        STATE_FILE.parent.mkdir(exist_ok=True)
        state = {
            "recording": self.recording,
            "session_dir": str(self.session_dir) if self.session_dir else None,
            "start_time": self.start_time,
            "pid": os.getpid()
        }
        # P0 fix (Data Integrity): atomic write via temp file + rename
        temp_file = STATE_FILE.with_suffix('.tmp')
        temp_file.write_text(json.dumps(state))
        temp_file.replace(STATE_FILE)  # Atomic on POSIX
    
    def _clear_state(self):
        """Clear state file after normal stop."""
        STATE_FILE.unlink(missing_ok=True)
    
    def _handle_signal(self, signum, frame):
        """Handle shutdown signals gracefully."""
        if self.recording:
            self.stop_recording()
        rumps.quit_application()
    
    def _cleanup_on_exit(self):
        """Cleanup on normal exit."""
        if self.recording:
            self.stop_recording()
        self._clear_state()
    
    def _build_menu(self):
        """Build the menu with toggle options."""
        self.start_item = rumps.MenuItem("▶️ Start Recording", callback=self.toggle_recording, key="r")
        
        # Screen submenu with monitor selection
        self.screen_menu = rumps.MenuItem("📺 Screen")
        self.screen_off_item = rumps.MenuItem("Off", callback=self._select_screen_off)
        self.screen_all_item = rumps.MenuItem("All Screens", callback=self._select_screen_all)
        self.screen_all_item.state = True  # default
        self.screen_menu.update([self.screen_off_item, self.screen_all_item])
        self._screen_items = {}
        for i, (idx, name) in enumerate(self._available_screens):
            item = rumps.MenuItem(name, callback=self._make_screen_selector(i))
            self._screen_items[i] = item
            self.screen_menu[name] = item

        self.camera_toggle = rumps.MenuItem("📷 Camera", callback=self._toggle_camera)
        self.camera_toggle.state = self.settings["record_camera"]

        self.audio_toggle = rumps.MenuItem("🔊 System Audio", callback=self._toggle_audio)
        self.audio_toggle.state = self.settings["record_audio"]
        
        self.mic_toggle = rumps.MenuItem("🎤 Microphone", callback=self._toggle_mic)
        self.mic_toggle.state = self.settings["record_mic"]
        
        self.bt_toggle = rumps.MenuItem("📶 Bluetooth", callback=self._toggle_bluetooth)
        self.bt_toggle.state = self.settings["record_bluetooth"]

        self.ctx_toggle = rumps.MenuItem("🌐 Context", callback=self._toggle_context)
        self.ctx_toggle.state = self.settings["record_context"]

        self.gps_toggle = rumps.MenuItem("📍 GPS", callback=self._toggle_gps)
        self.gps_toggle.state = self.settings["record_gps"]

        self.label_toggle = rumps.MenuItem("🏷 Label Panel", callback=self._toggle_labels)
        self.label_toggle.state = self.settings["show_labels"]

        self.overlay_toggle = rumps.MenuItem("📡 BLE Overlay", callback=self._toggle_overlay)
        self.overlay_toggle.state = self.settings["overlay_enabled"]

        # Status items
        self.status_item = rumps.MenuItem("Status: Idle")
        self.duration_item = rumps.MenuItem("Duration: --:--")
        self.size_item = rumps.MenuItem("Size: --")
        
        self.menu = [
            self.start_item,
            None,  # Separator
            "Recording Options:",
            self.screen_menu,
            self.camera_toggle,
            self.audio_toggle,
            self.mic_toggle,
            self.bt_toggle,
            self.ctx_toggle,
            self.gps_toggle,
            self.label_toggle,
            self.overlay_toggle,
            None,
            self.status_item,
            self.duration_item,
            self.size_item,
            None,
            rumps.MenuItem("📁 Open Recordings", callback=self.open_recordings),
            rumps.MenuItem("⚙️ Settings", callback=self.open_settings),
            rumps.MenuItem("❓ About", callback=self.show_about),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app, key="q"),
        ]
    
    def _clear_screen_checks(self):
        """Clear all screen menu checkmarks."""
        self.screen_off_item.state = False
        self.screen_all_item.state = False
        for item in self._screen_items.values():
            item.state = False

    def _select_screen_off(self, sender):
        if not self.recording:
            self._clear_screen_checks()
            sender.state = True
            self.settings["record_screen"] = False
            self._persist_settings()

    def _select_screen_all(self, sender):
        if not self.recording:
            self._clear_screen_checks()
            sender.state = True
            self.settings["record_screen"] = True
            self.settings["screen_selection"] = "all"
            self._persist_settings()

    def _make_screen_selector(self, screen_index):
        def callback(sender):
            if not self.recording:
                self._clear_screen_checks()
                sender.state = True
                self.settings["record_screen"] = True
                self.settings["screen_selection"] = screen_index
                self._persist_settings()
        return callback

    def _toggle_camera(self, sender):
        if not self.recording:
            self.settings["record_camera"] = not self.settings["record_camera"]
            sender.state = self.settings["record_camera"]
            self._persist_settings()

    def _toggle_audio(self, sender):
        if not self.recording:
            self.settings["record_audio"] = not self.settings["record_audio"]
            sender.state = self.settings["record_audio"]
            self._persist_settings()
    
    def _toggle_mic(self, sender):
        if not self.recording:
            self.settings["record_mic"] = not self.settings["record_mic"]
            sender.state = self.settings["record_mic"]
            self._persist_settings()
    
    def _toggle_bluetooth(self, sender):
        if not self.recording:
            self.settings["record_bluetooth"] = not self.settings["record_bluetooth"]
            sender.state = self.settings["record_bluetooth"]
            self._persist_settings()

    def _toggle_context(self, sender):
        if not self.recording:
            self.settings["record_context"] = not self.settings["record_context"]
            sender.state = self.settings["record_context"]
            self._persist_settings()

    def _toggle_gps(self, sender):
        if not self.recording:
            self.settings["record_gps"] = not self.settings["record_gps"]
            sender.state = self.settings["record_gps"]
            self._persist_settings()

    def _toggle_labels(self, sender):
        if not self.recording:
            self.settings["show_labels"] = not self.settings["show_labels"]
            sender.state = self.settings["show_labels"]
            self._persist_settings()

    def _toggle_overlay(self, sender):
        self.settings["overlay_enabled"] = not self.settings["overlay_enabled"]
        sender.state = self.settings["overlay_enabled"]
        self.config.setdefault("overlay", {})["enabled"] = self.settings["overlay_enabled"]
        self._persist_settings()

        if self.settings["overlay_enabled"]:
            self._show_overlay()
            self._ensure_ble_scanning()
        else:
            self._hide_overlay()
            self._stop_standalone_ble()

    def _deferred_show_overlay(self, sender):
        """Show overlay after RunLoop has started (one-shot timer)."""
        sender.stop()
        self._show_overlay()
        self._ensure_ble_scanning()

    def _show_overlay(self):
        """Show the BLE overlay panel."""
        if not self.overlay_panel:
            overlay_cfg = self.config.get("overlay", {})
            overlay_cfg["rssi_threshold"] = self.config.get("bluetooth", {}).get("rssi_threshold", -70)
            self.overlay_panel = BLEOverlayPanel(config=overlay_cfg, on_close=self._on_overlay_closed)
        self.overlay_panel.show()

    def _hide_overlay(self):
        """Hide the BLE overlay panel."""
        if self.overlay_panel:
            self.overlay_panel.hide()

    def _on_overlay_closed(self):
        """Called when user closes overlay via X button."""
        self.settings["overlay_enabled"] = False
        self.config.setdefault("overlay", {})["enabled"] = False
        self.overlay_toggle.state = False
        self._stop_standalone_ble()
        self._persist_settings()

    def _ensure_ble_scanning(self):
        """Ensure BLE scanning is active for overlay (standalone if not recording)."""
        if self.recording and self.bluetooth_monitor:
            return  # recording's BLE monitor already running
        if self._standalone_ble and self._standalone_ble.running:
            return  # already running

        self._standalone_ble = BluetoothMonitor(
            callback=lambda evt: None,  # no event logging
            scan_interval=self.config.get("bluetooth", {}).get("scan_interval", 1.0),
            anonymize=False,
            raw_callback=self._on_overlay_ble_event,
        )
        self._standalone_ble.start()
        logger.info("Standalone BLE scanning started for overlay")

    def _stop_standalone_ble(self):
        """Stop standalone BLE monitor (only if overlay is off and not recording)."""
        if self._standalone_ble:
            self._standalone_ble.stop()
            self._standalone_ble = None
            logger.info("Standalone BLE scanning stopped")

    def _on_overlay_ble_event(self, raw_event: dict):
        """Raw BLE callback for overlay (from standalone or recording monitor)."""
        if self.overlay_panel:
            self.overlay_panel.on_ble_event(raw_event)

    def _run_post_processing(self, session_dir: Path):
        """Launch Claude Code in a new Terminal tab to process the recording."""
        pp_config = self.config.get("post_processing", {})
        if not pp_config.get("enabled", False):
            return

        session_path = str(session_dir)
        cmd_template = pp_config.get("command", 'claude "/rec-processor {path}"')
        cmd = cmd_template.replace("{path}", session_path)

        try:
            # Use osascript to open a new Terminal tab and run the command
            apple_script = (
                'tell application "Terminal"\n'
                '    activate\n'
                f'    do script "{cmd}"\n'
                'end tell'
            )
            subprocess.Popen(
                ["osascript", "-e", apple_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"Post-processing launched: {cmd}")
        except Exception as e:
            logger.error(f"Failed to launch post-processing: {e}")
            rumps.notification(
                title="macOS Recorder",
                subtitle="Post-processing Error",
                message=f"Claude 실행 실패: {e}"
            )

    def _sync_ble_monitor_state(self):
        """Sync BLE monitor state based on recording/overlay status."""
        if self.settings["overlay_enabled"]:
            if not self.recording:
                self._ensure_ble_scanning()
            # If recording, recording's monitor handles it via raw_callback
        else:
            if not self.recording:
                self._stop_standalone_ble()

    def _on_settings_changed(self, key_path: str, value):
        """Callback from SettingsPanel when a setting changes."""
        # Update config by dotted key path
        keys = key_path.split(".")
        obj = self.config
        for k in keys[:-1]:
            obj = obj.setdefault(k, {})
        obj[keys[-1]] = value

        # Sync settings dict for relevant keys
        sync_map = {
            "recording.camera": "record_camera",
            "audio.system_audio": "record_audio",
            "audio.microphone": "record_mic",
            "bluetooth.enabled": "record_bluetooth",
            "bluetooth.anonymize": "anonymize_bluetooth",
            "context.enabled": "record_context",
            "gps.enabled": "record_gps",
            "labeling.enabled": "show_labels",
            "overlay.enabled": "overlay_enabled",
            "recording.fps": "fps",
        }
        if key_path in sync_map:
            self.settings[sync_map[key_path]] = value

        # Persist to config.yaml
        self._persist_settings()

        # Propagate overlay config changes
        if key_path.startswith("overlay.") or key_path == "bluetooth.rssi_threshold":
            if self.overlay_panel:
                overlay_cfg = self.config.get("overlay", {})
                overlay_cfg["rssi_threshold"] = self.config.get("bluetooth", {}).get("rssi_threshold", -70)
                self.overlay_panel.update_config(overlay_cfg)

        # Handle overlay toggle from settings
        if key_path == "overlay.enabled":
            self.overlay_toggle.state = value
            if value:
                self._show_overlay()
                self._ensure_ble_scanning()
            else:
                self._hide_overlay()
                self._stop_standalone_ble()

        # Sync menu checkmarks
        menu_sync = {
            "recording.camera": "camera_toggle",
            "audio.system_audio": "audio_toggle",
            "audio.microphone": "mic_toggle",
            "bluetooth.enabled": "bt_toggle",
            "context.enabled": "ctx_toggle",
            "gps.enabled": "gps_toggle",
            "labeling.enabled": "label_toggle",
        }
        if key_path in menu_sync:
            toggle = getattr(self, menu_sync[key_path], None)
            if toggle:
                toggle.state = value

    def _persist_settings(self):
        """P0 fix (Config): Save UI settings to config file."""
        try:
            import yaml
            config_path = Path(__file__).parent / "config.yaml"
            
            # Update config with current settings
            self.config["recording"]["camera"] = self.settings["record_camera"]
            self.config["audio"]["system_audio"] = self.settings["record_audio"]
            self.config["audio"]["microphone"] = self.settings["record_mic"]
            self.config["bluetooth"]["enabled"] = self.settings["record_bluetooth"]
            self.config["context"]["enabled"] = self.settings["record_context"]
            self.config.setdefault("gps", {})["enabled"] = self.settings["record_gps"]
            self.config.setdefault("labeling", {})["enabled"] = self.settings["show_labels"]
            self.config.setdefault("overlay", {})["enabled"] = self.settings.get("overlay_enabled", False)

            with open(config_path, 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
            
            logger.info("Settings persisted to config.yaml")
        except Exception as e:
            logger.warning(f"Failed to persist settings: {e}")
    
    def _check_startup_requirements(self):
        """Check permissions and consent on startup."""
        # Check consent
        if self.config["privacy"].get("require_consent", True):
            if not self.consent_manager.has_consent():
                if not self.consent_manager.request_consent():
                    rumps.notification(
                        title="macOS Recorder",
                        subtitle="",
                        message="동의가 필요합니다. 앱을 다시 시작하세요."
                    )
        
        # Check permissions
        permissions = PermissionChecker.validate_all()
        missing = [k for k, v in permissions.items() if not v]
        
        if missing:
            self._show_permission_alert(missing)
    
    def _show_permission_alert(self, missing: list):
        """Show alert for missing permissions."""
        response = rumps.alert(
            title="🔐 권한 필요",
            message=(
                f"다음 권한이 필요합니다:\n\n"
                f"• {', '.join(missing)}\n\n"
                "시스템 환경설정을 열까요?"
            ),
            ok="설정 열기",
            cancel="나중에"
        )
        
        if response == 1:
            PermissionChecker.open_privacy_settings()
    
    def toggle_recording(self, sender):
        """Start or stop recording."""
        if not self.recording:
            self.start_recording()
        else:
            self.stop_recording()
    
    def start_recording(self):
        """Initialize and start all recording components."""
        # Validate at least one recording option
        if not any([
            self.settings["record_screen"],
            self.settings["record_camera"],
            self.settings["record_audio"],
            self.settings["record_mic"],
            self.settings["record_bluetooth"],
            self.settings["record_context"],
            self.settings["record_gps"],
        ]):
            rumps.notification(
                title="macOS Recorder",
                subtitle="",
                message="최소 하나의 녹화 옵션을 선택하세요."
            )
            return
        
        self.recording = True
        self.start_time = time.time()
        
        # P0 fix (Real-time): Create reference timestamp for A/V sync
        self._reference_time = {
            "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
            "iso": datetime.now().isoformat()
        }
        
        # Create session directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.output_dir / f"recording_{timestamp}"
        self.session_dir.mkdir(exist_ok=True)
        secure_directory(self.session_dir)
        
        # Write reference timestamp to session (for post-processing sync)
        ref_file = self.session_dir / "reference_time.json"
        ref_file.write_text(json.dumps(self._reference_time, indent=2))
        secure_file(ref_file)
        
        # Open event log
        self.event_file = open(self.session_dir / "events.jsonl", "w")
        
        # Prevent sleep
        self.sleep_inhibitor = SleepInhibitor()
        self.sleep_inhibitor.start()

        # Switch audio output to multi-output device for recording
        if self.settings["record_audio"]:
            self.audio_switcher = AudioOutputSwitcher()
            self.audio_switcher.switch_to_recording()

        errors = []
        
        # Start screen recording
        if self.settings["record_screen"]:
            # Re-detect screens for fresh device indices (avfoundation indices shift on device changes)
            self._available_screens = find_all_screens() or [(1, "Default")]
            selection = self.settings.get("screen_selection", "all")
            if selection == "all":
                screens = self._available_screens
            else:
                idx_sel = int(selection)
                screens = [self._available_screens[idx_sel]] if idx_sel < len(self._available_screens) else self._available_screens[:1]

            for i, (idx, name) in enumerate(screens):
                filename = "screen.mp4" if len(screens) == 1 else f"screen_{i}.mp4"
                rec = ScreenRecorder(
                    output_path=self.session_dir / filename,
                    fps=self.settings["fps"],
                    monitor_idx=idx
                )
                if rec.start():
                    self.screen_recorders.append(rec)
                else:
                    errors.append(f"Screen {i} ({name}): {rec.get_error()}")
        
        # Start camera recording
        if self.settings["record_camera"]:
            self.camera_recorder = CameraRecorder(
                output_path=self.session_dir / "camera.mp4",
                fps=self.settings["fps"]
            )
            if not self.camera_recorder.start():
                errors.append(f"Camera: {self.camera_recorder.get_error()}")

        # Start audio recording (system audio)
        if self.settings["record_audio"]:
            self.audio_recorder = AudioRecorder(
                output_path=self.session_dir / "audio.wav",
                source="system",
                device_name=self.config["audio"].get("system_device")
            )
            self.audio_recorder.start()

        # Start microphone recording
        if self.settings["record_mic"]:
            self.mic_recorder = AudioRecorder(
                output_path=self.session_dir / "mic.wav",
                source="microphone",
                device_name=self.config["audio"].get("mic_device")
            )
            self.mic_recorder.start()
        
        # Start Bluetooth monitoring
        if self.settings["record_bluetooth"]:
            # Stop standalone BLE if running (avoid two scanners)
            self._stop_standalone_ble()

            self.bluetooth_monitor = BluetoothMonitor(
                callback=self._on_bluetooth_event,
                anonymize=self.settings["anonymize_bluetooth"],
                raw_callback=self._on_overlay_ble_event if self.settings.get("overlay_enabled") else None,
            )
            self.bluetooth_monitor.start()

        # Start context monitoring
        if self.settings["record_context"]:
            self.context_monitor = ContextMonitor(
                callback=self._on_context_event,
                poll_interval=self.config["context"].get("poll_interval", 10.0)
            )
            self.context_monitor.start()

        # Start GPS monitoring
        if self.settings["record_gps"]:
            self.location_monitor = LocationMonitor(
                callback=self._on_location_event,
                poll_interval=self.config.get("gps", {}).get("poll_interval", 60.0)
            )
            if not self.location_monitor.start():
                errors.append(f"GPS: {self.location_monitor.get_error()}")

        # Show label panel
        if self.settings["show_labels"]:
            self.label_panel = LabelPanel(log_event_callback=self._log_event)
            self.label_panel.show()

        # Fetch calendar context (optional, non-blocking)
        self._get_calendar_context()

        # Log recording start
        self._log_event("recording", {
            "action": "start",
            "options": {k: v for k, v in self.settings.items()}
        })
        
        # P0 fix: Save state for crash recovery
        self._save_state()
        
        # Update UI
        self.start_item.title = "⏹️ Stop Recording"
        self.title = "🔴"
        self.status_item.title = "Status: Recording..."
        self.duration_timer.start()
        
        # Disable toggles during recording
        self.screen_off_item.set_callback(None)
        self.screen_all_item.set_callback(None)
        for item in self._screen_items.values():
            item.set_callback(None)
        self.camera_toggle.set_callback(None)
        self.audio_toggle.set_callback(None)
        self.mic_toggle.set_callback(None)
        self.bt_toggle.set_callback(None)
        self.ctx_toggle.set_callback(None)
        self.gps_toggle.set_callback(None)
        self.label_toggle.set_callback(None)

        if errors:
            logger.warning(f"Recording started with errors: {'; '.join(errors)}")
    
    def stop_recording(self):
        """Stop all recording components and save files."""
        self.recording = False
        self.duration_timer.stop()
        
        self._log_event("recording", {"action": "stop"})
        
        # Stop all components
        for rec in self.screen_recorders:
            rec.stop()
        self.screen_recorders = []
        
        if self.camera_recorder:
            self.camera_recorder.stop()
            self.camera_recorder = None

        if self.audio_recorder:
            self.audio_recorder.stop()
            self.audio_recorder = None
        
        if self.mic_recorder:
            self.mic_recorder.stop()
            self.mic_recorder = None
        
        if self.bluetooth_monitor:
            self.bluetooth_monitor.stop()
            self.bluetooth_monitor = None

        if self.context_monitor:
            self.context_monitor.stop()
            self.context_monitor = None

        if self.location_monitor:
            self.location_monitor.stop()
            self.location_monitor = None

        if self.label_panel:
            self.label_panel.hide()
            self.label_panel = None

        if self.sleep_inhibitor:
            self.sleep_inhibitor.stop()
            self.sleep_inhibitor = None

        # Restore original audio output
        if self.audio_switcher:
            self.audio_switcher.restore()
            self.audio_switcher = None
        
        # P0 fix: Flush remaining events before closing
        self._flush_events()
        
        # Close event log
        if self.event_file:
            self.event_file.close()
            secure_file(self.session_dir / "events.jsonl")
            self.event_file = None
        
        # P1 fix (Data Integrity): Create completion marker
        if self.session_dir:
            completion_marker = self.session_dir / "COMPLETE"
            completion_data = {
                "completed_at": datetime.now().isoformat(),
                "duration_seconds": int(time.time() - self.start_time) if self.start_time else 0,
                "files": [f.name for f in self.session_dir.iterdir() if f.is_file()]
            }
            completion_marker.write_text(json.dumps(completion_data, indent=2))
            secure_file(completion_marker)
        
        # P0 fix: Clear state file after normal stop
        self._clear_state()
        
        # Calculate total size
        total_size = sum(
            f.stat().st_size for f in self.session_dir.iterdir() if f.is_file()
        ) if self.session_dir else 0
        size_mb = total_size / (1024 * 1024)
        
        # Update UI
        self.start_item.title = "▶️ Start Recording"
        self.title = "⚫"
        self.status_item.title = "Status: Idle"
        self.duration_item.title = "Duration: --:--"
        self.size_item.title = "Size: --"
        
        # Re-enable toggles
        self.screen_off_item.set_callback(self._select_screen_off)
        self.screen_all_item.set_callback(self._select_screen_all)
        for i, item in self._screen_items.items():
            item.set_callback(self._make_screen_selector(i))
        self.camera_toggle.set_callback(self._toggle_camera)
        self.audio_toggle.set_callback(self._toggle_audio)
        self.mic_toggle.set_callback(self._toggle_mic)
        self.bt_toggle.set_callback(self._toggle_bluetooth)
        self.ctx_toggle.set_callback(self._toggle_context)
        self.gps_toggle.set_callback(self._toggle_gps)
        self.label_toggle.set_callback(self._toggle_labels)
        
        session_name = self.session_dir.name if self.session_dir else "unknown"
        logger.info(f"Recording saved: {session_name} ({size_mb:.1f} MB)")

        # Post-processing: launch Claude in new Terminal tab
        if self.session_dir:
            self._run_post_processing(self.session_dir)

        # P2 fix (Memory): clear session references
        self.session_dir = None
        self.start_time = None
        self._reference_time = None

        # Restart standalone BLE if overlay is still active
        self._sync_ble_monitor_state()
    
    def _log_event(self, event_type: str, data: dict):
        """Log an event with timestamp (buffered for performance)."""
        event = {
            "ts": time.time_ns(),
            "ts_monotonic": time.monotonic_ns(),  # P0 fix: add monotonic timestamp
            "type": event_type,
            **data
        }
        
        with self._event_buffer_lock:
            # P1 fix (Memory): enforce buffer limit to prevent OOM
            if len(self._event_buffer) >= MAX_EVENT_BUFFER:
                # Keep recording events, drop oldest regular events
                important = [e for e in self._event_buffer if e.get('type') == 'recording']
                self._event_buffer = important[-100:] + self._event_buffer[-MAX_EVENT_BUFFER//2:]
                logger.warning(f"Event buffer overflow, dropped old events")
            
            self._event_buffer.append(event)
            # P0 fix: flush every 100 events, on important events, OR every 1 second
            should_flush = (
                len(self._event_buffer) >= 100 or 
                event_type == "recording" or  # Always flush start/stop
                (time.monotonic() - getattr(self, '_last_flush_time', 0)) >= 1.0
            )
        
        if should_flush:
            self._flush_events()
    
    def _flush_events(self):
        """Flush buffered events to file with error recovery."""
        if not self.event_file:
            return
        
        # P1 fix (Concurrency): update _last_flush_time inside lock
        with self._event_buffer_lock:
            events_to_write = self._event_buffer.copy()
            self._event_buffer.clear()
            self._last_flush_time = time.monotonic()
        
        # File I/O outside lock to avoid blocking
        failed_events = []
        for event in events_to_write:
            try:
                self.event_file.write(json.dumps(event) + "\n")
            except (IOError, OSError) as e:
                logger.error(f"Failed to write event: {e}")
                failed_events.append(event)
        
        try:
            self.event_file.flush()
        except (IOError, OSError) as e:
            logger.error(f"Failed to flush events: {e}")
        
        # Re-add failed events to buffer for retry
        if failed_events:
            with self._event_buffer_lock:
                self._event_buffer.extend(failed_events)
    
    def _on_bluetooth_event(self, bt_event: dict):
        """Callback for Bluetooth events."""
        self._log_event("bluetooth", bt_event)
        if self.label_panel:
            self.label_panel.update_devices(bt_event)

    def _on_location_event(self, location: dict):
        """Callback for GPS location events."""
        self._log_event("location", location)

    def _on_context_event(self, context: dict):
        """Callback for context monitoring events."""
        self._log_event("context", context)

    def _get_calendar_context(self):
        """Get current/upcoming calendar event (optional, non-blocking)."""
        try:
            calendar_script = os.path.expanduser(
                "~/.claude/skills/calendar-reader/scripts/read_calendar.py"
            )
            if not os.path.exists(calendar_script):
                return

            result = subprocess.run(
                [os.path.expanduser("~/.claude/.venv/bin/python"),
                 calendar_script,
                 "--days=1", "--format=json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                events = json.loads(result.stdout)
                now = datetime.now()
                # Filter events within ±15 minutes of now
                relevant = []
                for ev in events if isinstance(events, list) else []:
                    try:
                        start_str = ev.get("start", {}).get("dateTime", "")
                        if start_str:
                            from datetime import timezone
                            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                            diff_min = abs((start_dt.replace(tzinfo=None) - now).total_seconds()) / 60
                            if diff_min <= 15:
                                relevant.append({
                                    "summary": ev.get("summary", ""),
                                    "start": start_str,
                                    "attendees": [
                                        a.get("email", "") for a in ev.get("attendees", [])
                                    ],
                                })
                    except Exception:
                        continue

                if relevant:
                    self._log_event("calendar", {"events": relevant})
        except Exception as e:
            logger.debug(f"Calendar context unavailable: {e}")
    
    def _update_duration(self, sender):
        """Update the duration and size display."""
        if self.recording and self.start_time:
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            hours, mins = divmod(mins, 60)
            
            if hours > 0:
                duration_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
            else:
                duration_str = f"{mins:02d}:{secs:02d}"
            
            self.duration_item.title = f"Duration: {duration_str}"
            self.title = f"🔴 {mins:02d}:{secs:02d}"
            
            # P0 fix: Update size only every 10 seconds to reduce I/O
            current_time = time.time()
            if current_time - self._last_size_update >= 10:
                self._last_size_update = current_time
                if self.session_dir and self.session_dir.exists():
                    try:
                        self._cached_size = sum(
                            f.stat().st_size for f in self.session_dir.iterdir() if f.is_file()
                        )
                    except:
                        pass
            
            size_mb = self._cached_size / (1024 * 1024)
            self.size_item.title = f"Size: {size_mb:.1f} MB"
    
    def open_settings(self, sender):
        """Open settings panel."""
        if not self.settings_panel:
            self.settings_panel = SettingsPanel(
                config=self.config,
                on_change=self._on_settings_changed,
            )
        else:
            self.settings_panel.update_config(self.config)
        self.settings_panel.show()
    
    def open_recordings(self, sender):
        """Open the recordings folder in Finder."""
        subprocess.run(["open", str(self.output_dir)])
    
    def show_about(self, sender):
        """Show about dialog."""
        rumps.alert(
            title="macOS Recorder",
            message=(
                f"Version {__version__}\n\n"
                "화면, 오디오, 마이크, 블루투스 신호를 녹화합니다.\n\n"
                "Inspired by ocap from the D2E project.\n"
                "https://github.com/kubony/macos-recorder"
            )
        )
    
    def quit_app(self, sender):
        """Clean up and quit the application."""
        if self.recording:
            self.stop_recording()

        self._hide_overlay()
        self._stop_standalone_ble()

        rumps.quit_application()


def main():
    """Entry point for the menu bar app."""
    app = RecorderApp()
    app.run()


if __name__ == "__main__":
    main()
