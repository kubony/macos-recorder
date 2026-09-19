"""
Floating BLE device overlay panel.
Shows nearby BLE devices with device-to-person mapping from DeviceRegistry.
Runs independently of recording — can be toggled from the menu bar.

Architecture: NSPanel + NSTableView, refreshed by NSTimer on main thread.
Thread safety: _devices dict protected by threading.Lock.
"""

import logging
import threading
import time

import objc
from Foundation import NSObject, NSMakeRect, NSTimer, NSRunLoop, NSDefaultRunLoopMode
from AppKit import (
    NSPanel, NSTextField, NSScrollView, NSTableView,
    NSTableColumn, NSFont, NSColor, NSScreen,
    NSFloatingWindowLevel, NSBackingStoreBuffered,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskUtilityWindow, NSWindowStyleMaskHUDWindow,
    NSLineBreakByTruncatingTail,
)

from ble_identify import DeviceRegistry

logger = logging.getLogger(__name__)

# Stale device timeout (seconds)
DEVICE_TIMEOUT = 30.0


class _OverlayDelegate(NSObject):
    """NSTableView data source + NSTimer target for BLE overlay."""

    def initWithPanel_(self, panel):
        self = objc.super(_OverlayDelegate, self).init()
        if self is None:
            return None
        self._panel = panel
        return self

    # --- NSTableViewDataSource ---

    def numberOfRowsInTableView_(self, table):
        return len(self._panel._display_list)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >= len(self._panel._display_list):
            return ""
        entry = self._panel._display_list[row]
        col_id = str(column.identifier())

        if col_id == "status":
            if entry.get("is_separator"):
                return ""
            return "\U0001f7e2" if entry.get("owner") else "\u26aa"

        if col_id == "name":
            if entry.get("is_separator"):
                return "\u2500\u2500 unmapped \u2500\u2500"
            if entry.get("owner"):
                return f"{entry['owner']} ({entry['device_name']})"
            return entry.get("device_name", "")

        if col_id == "rssi":
            if entry.get("is_separator"):
                return ""
            rssi = entry.get("rssi")
            return f"{rssi}dBm" if rssi is not None else ""

        return ""

    # --- NSTimer callback ---

    @objc.typedSelector(b'v@:@')
    def refreshTimer_(self, timer):
        self._panel._refresh_display()

    # --- NSWindowDelegate ---

    def windowWillClose_(self, notification):
        self._panel._on_panel_closed()


class BLEOverlayPanel:
    """Floating panel showing nearby BLE devices with person mapping."""

    def __init__(self, config: dict = None, on_close=None):
        self._config = config or {}
        self._devices = {}  # {address: {name, rssi, last_seen}}
        self._lock = threading.Lock()
        self._display_list = []  # filtered/sorted for table
        self._registry = DeviceRegistry()
        self._shown = False
        self._panel = None
        self._table_view = None
        self._delegate = None
        self._header_label = None
        self._timer = None
        self.on_close = on_close  # called when user clicks X

    def update_config(self, config: dict):
        """Update overlay config (called from settings changes)."""
        self._config = config
        if self._panel:
            opacity = config.get("opacity", 0.85)
            self._panel.setAlphaValue_(opacity)

    def on_ble_event(self, bt_event: dict):
        """BLE callback — update _devices dict (thread-safe)."""
        address = bt_event.get("address", "")
        name = bt_event.get("name", "Unknown")
        rssi = bt_event.get("rssi")
        if not address or rssi is None:
            return

        with self._lock:
            self._devices[address] = {
                "name": name,
                "rssi": rssi,
                "last_seen": time.time(),
            }

    def _refresh_display(self):
        """Rebuild display list from _devices (called on main thread by NSTimer)."""
        now = time.time()
        rssi_threshold = self._config.get("rssi_threshold",
                          self._config.get("bluetooth", {}).get("rssi_threshold", -70))
        show_unmapped = self._config.get("show_unmapped", False)
        max_devices = self._config.get("max_devices", 15)

        with self._lock:
            # Remove stale devices
            stale = [addr for addr, d in self._devices.items()
                     if now - d["last_seen"] > DEVICE_TIMEOUT]
            for addr in stale:
                del self._devices[addr]

            snapshot = dict(self._devices)

        mapped = []
        unmapped = []

        for address, info in snapshot.items():
            name = info["name"]
            # Normalize non-breaking spaces (BLE device names may contain U+00A0)
            name = name.replace("\xa0", " ")
            rssi = info["rssi"]

            # RSSI filter
            if rssi < rssi_threshold:
                continue

            # Ambient filter
            if self._registry.is_ambient(name):
                continue

            device_info = self._registry.lookup(name)
            owner = device_info["owner"] if device_info and device_info.get("owner") else None

            entry = {
                "address": address,
                "device_name": name,
                "rssi": rssi,
                "owner": owner,
            }

            if owner:
                mapped.append(entry)
            else:
                unmapped.append(entry)

        # Sort by RSSI descending (strongest first)
        mapped.sort(key=lambda e: e["rssi"], reverse=True)
        unmapped.sort(key=lambda e: e["rssi"], reverse=True)

        display = mapped[:max_devices]
        if show_unmapped and unmapped:
            remaining = max_devices - len(display)
            if remaining > 0:
                display.append({"is_separator": True})
                display.extend(unmapped[:remaining])

        self._display_list = display

        # Update header
        device_count = len(mapped) + (len(unmapped) if show_unmapped else 0)
        if self._header_label:
            self._header_label.setStringValue_(f"\U0001f4e1 BLE Devices  ({device_count})")

        # Reload table
        if self._table_view:
            self._table_view.reloadData()

    # --- Show / Hide ---

    def show(self):
        if self._shown:
            return
        try:
            self._registry = DeviceRegistry()  # reload on each show
            self._create_panel()
            self._panel.orderFrontRegardless()
            self._shown = True

            # Start refresh timer on main run loop
            self._delegate_obj = self._delegate
            interval = self._config.get("refresh_interval", 2.0)
            self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                interval, self._delegate, b'refreshTimer:', None, True
            )
            NSRunLoop.mainRunLoop().addTimer_forMode_(self._timer, NSDefaultRunLoopMode)

            logger.info("BLE overlay shown")
        except Exception as e:
            logger.error(f"Failed to show BLE overlay: {e}", exc_info=True)

    def hide(self):
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        if self._panel:
            self._panel.orderOut_(None)
            self._panel = None
        self._table_view = None
        self._delegate = None
        self._header_label = None
        self._shown = False
        self._display_list = []
        logger.info("BLE overlay hidden")

    def _on_panel_closed(self):
        """Called by delegate when user clicks X."""
        self.hide()
        if self.on_close:
            self.on_close()

    @property
    def is_shown(self) -> bool:
        return self._shown

    # --- Panel UI ---

    def _create_panel(self):
        style = (
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow | NSWindowStyleMaskHUDWindow
        )

        W = 280
        PAD = 8
        HEADER_H = 24
        TABLE_H = 300
        total_h = PAD + TABLE_H + 4 + HEADER_H + PAD

        # Position: top-right of main screen
        screen = NSScreen.mainScreen()
        if screen:
            visible = screen.visibleFrame()
            x = visible.origin.x + visible.size.width - W - 16
            y = visible.origin.y + visible.size.height - total_h - 16
        else:
            x, y = 100, 400

        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, total_h),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._panel.setTitle_("BLE Overlay")
        self._panel.setLevel_(NSFloatingWindowLevel)
        self._panel.setFloatingPanel_(True)
        self._panel.setBecomesKeyOnlyIfNeeded_(True)
        self._panel.setHidesOnDeactivate_(False)
        opacity = self._config.get("opacity", 0.85)
        self._panel.setAlphaValue_(opacity)

        self._delegate = _OverlayDelegate.alloc().initWithPanel_(self)
        self._panel.setDelegate_(self._delegate)
        content = self._panel.contentView()
        CW = W - PAD * 2

        # Build bottom-up (Cocoa y=0 is bottom)
        y_pos = PAD

        # --- Table ---
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, y_pos, CW, TABLE_H))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)  # no border
        scroll.setDrawsBackground_(False)

        self._table_view = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, CW, TABLE_H))
        self._table_view.setBackgroundColor_(NSColor.clearColor())
        self._table_view.setHeaderView_(None)  # no column headers
        self._table_view.setRowHeight_(22)

        # Status column (emoji indicator)
        status_col = NSTableColumn.alloc().initWithIdentifier_("status")
        status_col.setWidth_(22)
        status_col.setEditable_(False)
        self._table_view.addTableColumn_(status_col)

        # Name column
        name_col = NSTableColumn.alloc().initWithIdentifier_("name")
        name_col.setWidth_(CW - 22 - 60)
        name_col.setEditable_(False)
        self._table_view.addTableColumn_(name_col)

        # RSSI column
        rssi_col = NSTableColumn.alloc().initWithIdentifier_("rssi")
        rssi_col.setWidth_(58)
        rssi_col.setEditable_(False)
        self._table_view.addTableColumn_(rssi_col)

        self._table_view.setDataSource_(self._delegate)
        scroll.setDocumentView_(self._table_view)
        content.addSubview_(scroll)
        y_pos += TABLE_H + 4

        # --- Header ---
        self._header_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y_pos, CW, HEADER_H))
        self._header_label.setStringValue_("\U0001f4e1 BLE Devices  (0)")
        self._header_label.setEditable_(False)
        self._header_label.setBordered_(False)
        self._header_label.setDrawsBackground_(False)
        self._header_label.setFont_(NSFont.boldSystemFontOfSize_(13))
        self._header_label.setTextColor_(NSColor.labelColor())
        content.addSubview_(self._header_label)
