#!/usr/bin/env python3
"""macOS Screen Recorder - Karpathy-style minimal implementation."""

import rumps
import subprocess
import time
import json
import asyncio
import threading
from pathlib import Path
from datetime import datetime

# Config - that's it. No YAML.
OUTPUT_DIR = Path.home() / "Recordings"
FPS = 30


class Recorder(rumps.App):
    def __init__(self):
        super().__init__("⚫", quit_button=None)
        self.recording = False
        self.processes = []
        self.session_dir = None
        self.start_time = None
        self.bt_file = None
        self.bt_thread = None
        self.bt_running = False
        
        self.toggle_item = rumps.MenuItem("▶️ 녹화 시작", callback=self.toggle, key="r")
        self.menu = [
            self.toggle_item,
            None,
            rumps.MenuItem("📁 폴더 열기", callback=self.open_folder),
            rumps.MenuItem("종료", callback=self.quit_app),
        ]
        
        self.timer = rumps.Timer(self.update_title, 1)
    
    def toggle(self, _):
        if self.recording:
            self.stop()
        else:
            self.start()
    
    def start(self):
        OUTPUT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = OUTPUT_DIR / f"rec_{ts}"
        self.session_dir.mkdir()
        
        # Screen recording (Capture screen 0 = index 4)
        self.processes.append(subprocess.Popen([
            "ffmpeg", "-y", "-f", "avfoundation",
            "-capture_cursor", "1", "-framerate", str(FPS),
            "-i", "4:none",
            "-c:v", "h264_videotoolbox", "-b:v", "5M",
            str(self.session_dir / "screen.mp4")
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        # Camera recording (MacBook Pro 카메라 = index 0)
        self.processes.append(subprocess.Popen([
            "ffmpeg", "-y", "-f", "avfoundation",
            "-video_size", "1920x1080",
            "-framerate", str(FPS),
            "-i", "0:none",
            "-c:v", "h264_videotoolbox", "-b:v", "3M",
            str(self.session_dir / "camera.mp4")
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        # Microphone recording (MacBook Pro 마이크 = index 0, volume boost 3x)
        self.processes.append(subprocess.Popen([
            "ffmpeg", "-y", "-f", "avfoundation",
            "-i", ":0",
            "-af", "volume=5.0",
            "-c:a", "aac", "-b:a", "128k",
            str(self.session_dir / "mic.m4a")
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        # System audio (if BlackHole available)
        try:
            result = subprocess.run(
                ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=5
            )
            if "BlackHole" in result.stderr:
                self.processes.append(subprocess.Popen([
                    "ffmpeg", "-y", "-f", "avfoundation",
                    "-i", ":BlackHole 2ch",
                    "-c:a", "aac", "-b:a", "128k",
                    str(self.session_dir / "audio.m4a")
                ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except:
            pass
        
        # Prevent sleep
        self.processes.append(subprocess.Popen(["caffeinate", "-dims"]))

        # Wait for ffmpeg to actually start recording
        mp4_path = self.session_dir / "screen.mp4"
        for _ in range(50):  # up to 5 seconds
            time.sleep(0.1)
            if mp4_path.exists() and mp4_path.stat().st_size > 0:
                break

        # Bluetooth log
        self.bt_file = open(self.session_dir / "bluetooth.jsonl", "w")
        self.log_bt({"type": "start"})

        # Bluetooth monitoring
        self.bt_running = True
        self.bt_thread = threading.Thread(target=self._bt_monitor, daemon=True)
        self.bt_thread.start()
        
        self.recording = True
        self.start_time = time.time()
        self.toggle_item.title = "⏹️ 녹화 중지"
        self.title = "🔴"
        self.timer.start()
        
        subprocess.run(["afplay", "/System/Library/Sounds/Blow.aiff"], capture_output=True)
    
    def stop(self):
        self.timer.stop()
        self.bt_running = False
        self.log_bt({"type": "stop", "duration": time.time() - self.start_time})

        # Stop all processes
        for p in self.processes:
            try:
                if p.stdin:
                    p.stdin.write(b'q')
                    p.stdin.flush()
                p.wait(timeout=5)
            except:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except:
                    p.kill()
        self.processes = []
        
        if self.bt_file:
            self.bt_file.close()
            self.bt_file = None
        
        self.recording = False
        self.toggle_item.title = "▶️ 녹화 시작"
        self.title = "⚫"
        
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], capture_output=True)
        rumps.notification("녹화 완료", "", str(self.session_dir.name))
    
    def log_bt(self, data: dict):
        if self.bt_file:
            event = {"ts": time.time_ns(), **data}
            self.bt_file.write(json.dumps(event) + "\n")
            self.bt_file.flush()
    
    def update_title(self, _):
        if self.recording and self.start_time:
            elapsed = int(time.time() - self.start_time)
            h, remainder = divmod(elapsed, 3600)
            m, s = divmod(remainder, 60)
            if h > 0:
                self.title = f"🔴 {h}:{m:02d}:{s:02d}"
            else:
                self.title = f"🔴 {m:02d}:{s:02d}"
    
    def open_folder(self, _):
        subprocess.run(["open", str(OUTPUT_DIR)])
    
    def quit_app(self, _):
        if self.recording:
            self.stop()
        rumps.quit_application()
    
    def _bt_monitor(self):
        """Bluetooth RSSI monitoring - minimal version."""
        try:
            from bleak import BleakScanner
        except ImportError:
            return  # No bleak? Skip it.
        
        def on_detection(device, advertisement_data):
            if not self.bt_running:
                return
            self.log_bt({
                "name": device.name or advertisement_data.local_name or "Unknown",
                "address": device.address,
                "rssi": advertisement_data.rssi,
                "tx_power": advertisement_data.tx_power,
                "service_uuids": advertisement_data.service_uuids or [],
                "manufacturer_data": {str(k): v.hex() for k, v in advertisement_data.manufacturer_data.items()} if advertisement_data.manufacturer_data else {},
            })

        async def scan():
            scanner = BleakScanner(detection_callback=on_detection)
            await scanner.start()
            while self.bt_running:
                await asyncio.sleep(0.5)
            await scanner.stop()
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(scan())
        finally:
            loop.close()


if __name__ == "__main__":
    Recorder().run()
