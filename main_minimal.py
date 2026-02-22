#!/usr/bin/env python3
"""macOS Screen Recorder - Karpathy-style minimal implementation."""

import rumps
import subprocess
import time
import json
import asyncio
import hashlib
import threading
from pathlib import Path
from datetime import datetime

# Config - that's it. No YAML.
OUTPUT_DIR = Path.home() / "Recordings"
FPS = 30
BT_SALT = "karpathy"  # 익명화용 salt (한 줄이면 충분)


class Recorder(rumps.App):
    def __init__(self):
        super().__init__("⚫", quit_button=None)
        self.recording = False
        self.processes = []
        self.session_dir = None
        self.start_time = None
        self.event_file = None
        self.bt_thread = None
        self.bt_running = False
        
        self.menu = [
            rumps.MenuItem("▶️ 녹화 시작", callback=self.toggle, key="r"),
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
        
        # Screen recording (ffmpeg)
        self.processes.append(subprocess.Popen([
            "ffmpeg", "-y", "-f", "avfoundation",
            "-capture_cursor", "1", "-framerate", str(FPS),
            "-i", "1:none",
            "-c:v", "h264_videotoolbox", "-b:v", "5M",
            str(self.session_dir / "screen.mp4")
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        
        # System audio (if BlackHole available)
        try:
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
        
        # Event log (direct write, no buffering)
        self.event_file = open(self.session_dir / "events.jsonl", "w")
        self.log_event("recording", {"action": "start"})
        
        # Bluetooth monitoring
        self.bt_running = True
        self.bt_thread = threading.Thread(target=self._bt_monitor, daemon=True)
        self.bt_thread.start()
        
        self.recording = True
        self.start_time = time.time()
        self.menu["▶️ 녹화 시작"].title = "⏹️ 녹화 중지"
        self.title = "🔴"
        self.timer.start()
        
        subprocess.run(["afplay", "/System/Library/Sounds/Blow.aiff"], capture_output=True)
    
    def stop(self):
        self.timer.stop()
        self.bt_running = False
        self.log_event("recording", {"action": "stop", "duration": time.time() - self.start_time})
        
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
        
        if self.event_file:
            self.event_file.close()
            self.event_file = None
        
        self.recording = False
        self.menu["⏹️ 녹화 중지"].title = "▶️ 녹화 시작"
        self.title = "⚫"
        
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], capture_output=True)
        rumps.notification("녹화 완료", "", str(self.session_dir.name))
    
    def log_event(self, event_type: str, data: dict):
        if self.event_file:
            event = {"ts": time.time_ns(), "type": event_type, **data}
            self.event_file.write(json.dumps(event) + "\n")
            self.event_file.flush()
    
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
        
        def anonymize(name: str) -> str:
            return f"Device_{hashlib.sha256((BT_SALT + (name or 'unknown')).encode()).hexdigest()[:6]}"
        
        async def scan():
            while self.bt_running:
                try:
                    devices = await BleakScanner.discover(timeout=1.0)
                    for d in devices:
                        if d.rssi is not None:
                            self.log_event("bluetooth", {
                                "device": anonymize(d.name),
                                "rssi": d.rssi
                            })
                except:
                    pass
                await asyncio.sleep(1.0)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(scan())
        finally:
            loop.close()


if __name__ == "__main__":
    Recorder().run()
