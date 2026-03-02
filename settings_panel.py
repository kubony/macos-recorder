"""
macOS Settings panel for macos-recorder.
NSWindow + NSTabView with 4 tabs: General, Audio, BLE & Overlay, Privacy.

All control actions flow through _SettingsDelegate → on_change callback,
which propagates to main.py for config persistence and live updates.
"""

import logging

import objc
from Foundation import NSObject, NSMakeRect
from AppKit import (
    NSWindow, NSTabView, NSTabViewItem, NSTextField, NSButton,
    NSPopUpButton, NSSlider, NSFont, NSColor, NSOpenPanel, NSScreen,
    NSBackingStoreBuffered,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBezelStyleRounded, NSControlStateValueOn, NSControlStateValueOff,
    NSStepper,
)

logger = logging.getLogger(__name__)

# Layout constants
W = 520
H = 400
PAD = 20
LABEL_W = 140
CTRL_X = PAD + LABEL_W + 8
CTRL_W = W - CTRL_X - PAD
ROW_H = 28
GAP = 6


def _make_label(text, x, y, width=LABEL_W, height=ROW_H):
    """Create a right-aligned label."""
    label = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
    label.setStringValue_(text)
    label.setEditable_(False)
    label.setBordered_(False)
    label.setDrawsBackground_(False)
    label.setAlignment_(2)  # right-aligned
    label.setFont_(NSFont.systemFontOfSize_(13))
    return label


def _make_checkbox(title, x, y, width=CTRL_W, height=ROW_H):
    """Create a checkbox button."""
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
    btn.setButtonType_(3)  # NSSwitchButton
    btn.setTitle_(title)
    btn.setFont_(NSFont.systemFontOfSize_(13))
    return btn


def _make_popup(items, x, y, width=CTRL_W, height=ROW_H):
    """Create a popup button (dropdown)."""
    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(x, y, width, height), False)
    for item in items:
        popup.addItemWithTitle_(str(item))
    popup.setFont_(NSFont.systemFontOfSize_(13))
    return popup


def _make_separator(y, content_width):
    """Create a horizontal separator line."""
    sep = NSTextField.alloc().initWithFrame_(
        NSMakeRect(PAD, y, content_width, 1))
    sep.setEditable_(False)
    sep.setBordered_(True)
    sep.setDrawsBackground_(True)
    sep.setBackgroundColor_(NSColor.separatorColor())
    return sep


class _SettingsDelegate(NSObject):
    """Handles all control actions for the settings panel."""

    def initWithPanel_(self, panel):
        self = objc.super(_SettingsDelegate, self).init()
        if self is None:
            return None
        self._panel = panel
        return self

    @objc.typedSelector(b'v@:@')
    def onCheckbox_(self, sender):
        tag = sender.tag()
        value = sender.state() == NSControlStateValueOn
        key_map = {
            100: "recording.include_cursor",
            101: "recording.camera",
            200: "audio.system_audio",
            201: "audio.microphone",
            300: "bluetooth.enabled",
            301: "bluetooth.anonymize",
            310: "overlay.enabled",
            311: "overlay.show_unmapped",
            400: "privacy.require_consent",
            402: "privacy.anonymize_bluetooth",
            500: "post_processing.enabled",
        }
        key_path = key_map.get(tag)
        if key_path and self._panel.on_change:
            self._panel.on_change(key_path, value)

    @objc.typedSelector(b'v@:@')
    def onPopup_(self, sender):
        tag = sender.tag()
        if tag == 110:  # FPS
            fps_values = [10, 15, 24, 30, 60]
            idx = sender.indexOfSelectedItem()
            if 0 <= idx < len(fps_values):
                self._panel.on_change("recording.fps", fps_values[idx])
        elif tag == 111:  # Quality
            qualities = ["low", "medium", "high"]
            idx = sender.indexOfSelectedItem()
            if 0 <= idx < len(qualities):
                self._panel.on_change("recording.quality", qualities[idx])
        elif tag == 210:  # Mic device
            title = str(sender.titleOfSelectedItem())
            self._panel.on_change("audio.mic_device", title if title != "(default)" else None)
        elif tag == 211:  # Sample rate
            rates = [22050, 44100, 48000]
            idx = sender.indexOfSelectedItem()
            if 0 <= idx < len(rates):
                self._panel.on_change("audio.sample_rate", rates[idx])

    @objc.typedSelector(b'v@:@')
    def onSlider_(self, sender):
        tag = sender.tag()
        value = sender.floatValue()
        if tag == 320:  # RSSI threshold
            int_val = int(value)
            if self._panel._rssi_value_label:
                self._panel._rssi_value_label.setStringValue_(f"{int_val} dBm")
            # Debounce: only propagate on mouse-up
            from AppKit import NSApp, NSLeftMouseUp
            event = NSApp.currentEvent()
            if event and event.type() == NSLeftMouseUp:
                self._panel.on_change("bluetooth.rssi_threshold", int_val)
        elif tag == 321:  # Opacity
            rounded = round(value, 2)
            if self._panel._opacity_value_label:
                self._panel._opacity_value_label.setStringValue_(f"{rounded:.0%}")
            from AppKit import NSApp, NSLeftMouseUp
            event = NSApp.currentEvent()
            if event and event.type() == NSLeftMouseUp:
                self._panel.on_change("overlay.opacity", rounded)

    @objc.typedSelector(b'v@:@')
    def onStepper_(self, sender):
        tag = sender.tag()
        value = sender.floatValue()
        if tag == 330:  # Scan interval
            if self._panel._scan_interval_field:
                self._panel._scan_interval_field.setStringValue_(f"{value:.1f}")
            self._panel.on_change("bluetooth.scan_interval", value)
        elif tag == 410:  # Auto-delete days
            int_val = int(value)
            if self._panel._auto_delete_field:
                self._panel._auto_delete_field.setStringValue_(str(int_val))
            self._panel.on_change("privacy.auto_delete_days", int_val)

    @objc.typedSelector(b'v@:@')
    def onTextField_(self, sender):
        tag = sender.tag()
        text = str(sender.stringValue())
        if tag == 120:  # Output directory
            self._panel.on_change("output.directory", text)

    @objc.typedSelector(b'v@:@')
    def browseDirectory_(self, sender):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        if panel.runModal() == 1:
            url = panel.URLs()[0]
            path = str(url.path())
            if self._panel._output_dir_field:
                self._panel._output_dir_field.setStringValue_(path)
            if self._panel.on_change:
                self._panel.on_change("output.directory", path)


class SettingsPanel:
    """macOS-style settings window with tabbed interface."""

    def __init__(self, config: dict, on_change=None):
        self._config = config
        self.on_change = on_change  # Callable[[str, Any], None]
        self._window = None
        self._delegate = None
        self._shown = False

        # Control references for programmatic updates
        self._output_dir_field = None
        self._rssi_value_label = None
        self._opacity_value_label = None
        self._scan_interval_field = None
        self._auto_delete_field = None

    def show(self):
        if self._shown and self._window:
            self._window.makeKeyAndOrderFront_(None)
            return
        try:
            self._create_window()
            from AppKit import NSApp
            NSApp.activateIgnoringOtherApps_(True)
            self._window.makeKeyAndOrderFront_(None)
            self._shown = True
            logger.info("Settings panel shown")
        except Exception as e:
            logger.error(f"Failed to show settings: {e}", exc_info=True)

    def hide(self):
        if self._window:
            self._window.orderOut_(None)
        self._shown = False

    def update_config(self, config: dict):
        """Update internal config reference."""
        self._config = config

    def _create_window(self):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable

        # Center on screen
        screen = NSScreen.mainScreen()
        if screen:
            sf = screen.frame()
            x = sf.origin.x + (sf.size.width - W) / 2
            y = sf.origin.y + (sf.size.height - H) / 2
        else:
            x, y = 200, 200

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("Settings")
        self._window.setReleasedWhenClosed_(False)

        self._delegate = _SettingsDelegate.alloc().initWithPanel_(self)

        # Tab view
        tab_view = NSTabView.alloc().initWithFrame_(
            NSMakeRect(0, 0, W, H))

        tab_view.addTabViewItem_(self._create_general_tab())
        tab_view.addTabViewItem_(self._create_audio_tab())
        tab_view.addTabViewItem_(self._create_ble_tab())
        tab_view.addTabViewItem_(self._create_privacy_tab())

        self._window.contentView().addSubview_(tab_view)

    def _get(self, key_path: str, default=None):
        """Get a config value by dotted key path."""
        keys = key_path.split(".")
        obj = self._config
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
            else:
                return default
        return obj if obj is not None else default

    # --- General Tab ---

    def _create_general_tab(self):
        item = NSTabViewItem.alloc().initWithIdentifier_("general")
        item.setLabel_("General")
        view = item.view()
        tab_h = H - 60  # approximate tab content height
        y = tab_h - PAD - ROW_H

        # FPS
        view.addSubview_(_make_label("FPS:", PAD, y))
        fps_popup = _make_popup(["10", "15", "24", "30", "60"], CTRL_X, y, width=100)
        fps_popup.setTag_(110)
        fps_values = [10, 15, 24, 30, 60]
        current_fps = self._get("recording.fps", 30)
        if current_fps in fps_values:
            fps_popup.selectItemAtIndex_(fps_values.index(current_fps))
        fps_popup.setTarget_(self._delegate)
        fps_popup.setAction_(b'onPopup:')
        view.addSubview_(fps_popup)
        y -= ROW_H + GAP

        # Quality
        view.addSubview_(_make_label("Quality:", PAD, y))
        qual_popup = _make_popup(["low", "medium", "high"], CTRL_X, y, width=100)
        qual_popup.setTag_(111)
        qualities = ["low", "medium", "high"]
        current_q = self._get("recording.quality", "high")
        if current_q in qualities:
            qual_popup.selectItemAtIndex_(qualities.index(current_q))
        qual_popup.setTarget_(self._delegate)
        qual_popup.setAction_(b'onPopup:')
        view.addSubview_(qual_popup)
        y -= ROW_H + GAP

        # Include Cursor
        cursor_chk = _make_checkbox("Include Cursor", CTRL_X, y)
        cursor_chk.setTag_(100)
        cursor_chk.setState_(NSControlStateValueOn if self._get("recording.include_cursor", True) else NSControlStateValueOff)
        cursor_chk.setTarget_(self._delegate)
        cursor_chk.setAction_(b'onCheckbox:')
        view.addSubview_(cursor_chk)
        y -= ROW_H + GAP

        # Camera
        camera_chk = _make_checkbox("Camera", CTRL_X, y)
        camera_chk.setTag_(101)
        camera_chk.setState_(NSControlStateValueOn if self._get("recording.camera", True) else NSControlStateValueOff)
        camera_chk.setTarget_(self._delegate)
        camera_chk.setAction_(b'onCheckbox:')
        view.addSubview_(camera_chk)
        y -= ROW_H + GAP

        # Output Directory
        view.addSubview_(_make_label("Output:", PAD, y))
        self._output_dir_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X, y, CTRL_W - 80, ROW_H))
        self._output_dir_field.setStringValue_(self._get("output.directory", "~/Recordings"))
        self._output_dir_field.setFont_(NSFont.systemFontOfSize_(12))
        self._output_dir_field.setTag_(120)
        self._output_dir_field.setTarget_(self._delegate)
        self._output_dir_field.setAction_(b'onTextField:')
        view.addSubview_(self._output_dir_field)

        browse_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + CTRL_W - 75, y, 75, ROW_H))
        browse_btn.setTitle_("Browse...")
        browse_btn.setBezelStyle_(NSBezelStyleRounded)
        browse_btn.setTarget_(self._delegate)
        browse_btn.setAction_(b'browseDirectory:')
        view.addSubview_(browse_btn)
        y -= ROW_H + GAP + 4

        # Separator
        view.addSubview_(_make_separator(y + ROW_H // 2, W - PAD * 2))
        y -= GAP

        # Auto-process with Claude
        autoprocess_chk = _make_checkbox("Auto-process with Claude", CTRL_X, y)
        autoprocess_chk.setTag_(500)
        autoprocess_chk.setState_(
            NSControlStateValueOn if self._get("post_processing.enabled", False)
            else NSControlStateValueOff)
        autoprocess_chk.setTarget_(self._delegate)
        autoprocess_chk.setAction_(b'onCheckbox:')
        view.addSubview_(autoprocess_chk)

        hint = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X, y - 18, CTRL_W, 16))
        hint.setStringValue_("Runs /rec-processor in Terminal after recording stops")
        hint.setEditable_(False)
        hint.setBordered_(False)
        hint.setDrawsBackground_(False)
        hint.setFont_(NSFont.systemFontOfSize_(10))
        hint.setTextColor_(NSColor.secondaryLabelColor())
        view.addSubview_(hint)

        return item

    # --- Audio Tab ---

    def _create_audio_tab(self):
        item = NSTabViewItem.alloc().initWithIdentifier_("audio")
        item.setLabel_("Audio")
        view = item.view()
        tab_h = H - 60
        y = tab_h - PAD - ROW_H

        # System Audio
        sys_chk = _make_checkbox("System Audio", CTRL_X, y)
        sys_chk.setTag_(200)
        sys_chk.setState_(NSControlStateValueOn if self._get("audio.system_audio", True) else NSControlStateValueOff)
        sys_chk.setTarget_(self._delegate)
        sys_chk.setAction_(b'onCheckbox:')
        view.addSubview_(sys_chk)
        y -= ROW_H + GAP

        # Microphone
        mic_chk = _make_checkbox("Microphone", CTRL_X, y)
        mic_chk.setTag_(201)
        mic_chk.setState_(NSControlStateValueOn if self._get("audio.microphone", True) else NSControlStateValueOff)
        mic_chk.setTarget_(self._delegate)
        mic_chk.setAction_(b'onCheckbox:')
        view.addSubview_(mic_chk)
        y -= ROW_H + GAP

        # Mic Device
        view.addSubview_(_make_label("Mic Device:", PAD, y))
        mic_devices = ["(default)"]
        try:
            import sounddevice as sd
            for d in sd.query_devices():
                if d["max_input_channels"] > 0:
                    mic_devices.append(d["name"])
        except Exception:
            pass
        mic_popup = _make_popup(mic_devices, CTRL_X, y, width=CTRL_W)
        mic_popup.setTag_(210)
        current_mic = self._get("audio.mic_device")
        if current_mic and current_mic in mic_devices:
            mic_popup.selectItemWithTitle_(current_mic)
        mic_popup.setTarget_(self._delegate)
        mic_popup.setAction_(b'onPopup:')
        view.addSubview_(mic_popup)
        y -= ROW_H + GAP

        # Sample Rate
        view.addSubview_(_make_label("Sample Rate:", PAD, y))
        rate_popup = _make_popup(["22050", "44100", "48000"], CTRL_X, y, width=120)
        rate_popup.setTag_(211)
        rates = [22050, 44100, 48000]
        current_rate = self._get("audio.sample_rate", 44100)
        if current_rate in rates:
            rate_popup.selectItemAtIndex_(rates.index(current_rate))
        rate_popup.setTarget_(self._delegate)
        rate_popup.setAction_(b'onPopup:')
        view.addSubview_(rate_popup)

        return item

    # --- BLE & Overlay Tab ---

    def _create_ble_tab(self):
        item = NSTabViewItem.alloc().initWithIdentifier_("ble")
        item.setLabel_("BLE & Overlay")
        view = item.view()
        tab_h = H - 60
        y = tab_h - PAD - ROW_H

        # BLE Scan
        ble_chk = _make_checkbox("BLE Scan", CTRL_X, y)
        ble_chk.setTag_(300)
        ble_chk.setState_(NSControlStateValueOn if self._get("bluetooth.enabled", True) else NSControlStateValueOff)
        ble_chk.setTarget_(self._delegate)
        ble_chk.setAction_(b'onCheckbox:')
        view.addSubview_(ble_chk)
        y -= ROW_H + GAP

        # Scan Interval
        view.addSubview_(_make_label("Scan Interval:", PAD, y))
        self._scan_interval_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X, y, 50, ROW_H))
        scan_val = self._get("bluetooth.scan_interval", 1.0)
        self._scan_interval_field.setStringValue_(f"{scan_val:.1f}")
        self._scan_interval_field.setEditable_(False)
        self._scan_interval_field.setFont_(NSFont.systemFontOfSize_(13))
        view.addSubview_(self._scan_interval_field)

        scan_stepper = NSStepper.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + 55, y, 19, ROW_H))
        scan_stepper.setMinValue_(0.5)
        scan_stepper.setMaxValue_(10.0)
        scan_stepper.setIncrement_(0.5)
        scan_stepper.setFloatValue_(scan_val)
        scan_stepper.setTag_(330)
        scan_stepper.setTarget_(self._delegate)
        scan_stepper.setAction_(b'onStepper:')
        view.addSubview_(scan_stepper)

        sec_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + 80, y, 30, ROW_H))
        sec_label.setStringValue_("sec")
        sec_label.setEditable_(False)
        sec_label.setBordered_(False)
        sec_label.setDrawsBackground_(False)
        sec_label.setFont_(NSFont.systemFontOfSize_(12))
        view.addSubview_(sec_label)
        y -= ROW_H + GAP

        # Anonymize
        anon_chk = _make_checkbox("Anonymize", CTRL_X, y)
        anon_chk.setTag_(301)
        anon_chk.setState_(NSControlStateValueOn if self._get("bluetooth.anonymize", False) else NSControlStateValueOff)
        anon_chk.setTarget_(self._delegate)
        anon_chk.setAction_(b'onCheckbox:')
        view.addSubview_(anon_chk)
        y -= ROW_H + GAP

        # Separator
        view.addSubview_(_make_separator(y + ROW_H // 2, W - PAD * 2))
        y -= GAP

        # Overlay enable
        overlay_chk = _make_checkbox("Overlay", CTRL_X, y)
        overlay_chk.setTag_(310)
        overlay_chk.setState_(NSControlStateValueOn if self._get("overlay.enabled", False) else NSControlStateValueOff)
        overlay_chk.setTarget_(self._delegate)
        overlay_chk.setAction_(b'onCheckbox:')
        view.addSubview_(overlay_chk)
        y -= ROW_H + GAP

        # Show Unmapped
        unmapped_chk = _make_checkbox("Show Unmapped", CTRL_X, y)
        unmapped_chk.setTag_(311)
        unmapped_chk.setState_(NSControlStateValueOn if self._get("overlay.show_unmapped", False) else NSControlStateValueOff)
        unmapped_chk.setTarget_(self._delegate)
        unmapped_chk.setAction_(b'onCheckbox:')
        view.addSubview_(unmapped_chk)
        y -= ROW_H + GAP

        # RSSI Threshold
        view.addSubview_(_make_label("RSSI Threshold:", PAD, y))
        rssi_val = self._get("bluetooth.rssi_threshold", -70)
        rssi_slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(CTRL_X, y + 4, CTRL_W - 70, 20))
        rssi_slider.setMinValue_(-100)
        rssi_slider.setMaxValue_(-30)
        rssi_slider.setFloatValue_(float(rssi_val))
        rssi_slider.setTag_(320)
        rssi_slider.setTarget_(self._delegate)
        rssi_slider.setAction_(b'onSlider:')
        view.addSubview_(rssi_slider)

        self._rssi_value_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + CTRL_W - 65, y, 65, ROW_H))
        self._rssi_value_label.setStringValue_(f"{rssi_val} dBm")
        self._rssi_value_label.setEditable_(False)
        self._rssi_value_label.setBordered_(False)
        self._rssi_value_label.setDrawsBackground_(False)
        self._rssi_value_label.setFont_(NSFont.systemFontOfSize_(12))
        view.addSubview_(self._rssi_value_label)
        y -= ROW_H + GAP

        # Opacity
        view.addSubview_(_make_label("Opacity:", PAD, y))
        opacity_val = self._get("overlay.opacity", 0.85)
        opacity_slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(CTRL_X, y + 4, CTRL_W - 70, 20))
        opacity_slider.setMinValue_(0.3)
        opacity_slider.setMaxValue_(1.0)
        opacity_slider.setFloatValue_(float(opacity_val))
        opacity_slider.setTag_(321)
        opacity_slider.setTarget_(self._delegate)
        opacity_slider.setAction_(b'onSlider:')
        view.addSubview_(opacity_slider)

        self._opacity_value_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + CTRL_W - 65, y, 65, ROW_H))
        self._opacity_value_label.setStringValue_(f"{opacity_val:.0%}")
        self._opacity_value_label.setEditable_(False)
        self._opacity_value_label.setBordered_(False)
        self._opacity_value_label.setDrawsBackground_(False)
        self._opacity_value_label.setFont_(NSFont.systemFontOfSize_(12))
        view.addSubview_(self._opacity_value_label)

        return item

    # --- Privacy Tab ---

    def _create_privacy_tab(self):
        item = NSTabViewItem.alloc().initWithIdentifier_("privacy")
        item.setLabel_("Privacy")
        view = item.view()
        tab_h = H - 60
        y = tab_h - PAD - ROW_H

        # Require Consent
        consent_chk = _make_checkbox("Require Consent", CTRL_X, y)
        consent_chk.setTag_(400)
        consent_chk.setState_(NSControlStateValueOn if self._get("privacy.require_consent", True) else NSControlStateValueOff)
        consent_chk.setTarget_(self._delegate)
        consent_chk.setAction_(b'onCheckbox:')
        view.addSubview_(consent_chk)
        y -= ROW_H + GAP

        # Auto-Delete Days
        view.addSubview_(_make_label("Auto-Delete Days:", PAD, y))
        self._auto_delete_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X, y, 50, ROW_H))
        delete_val = self._get("privacy.auto_delete_days", 30)
        self._auto_delete_field.setStringValue_(str(delete_val))
        self._auto_delete_field.setEditable_(False)
        self._auto_delete_field.setFont_(NSFont.systemFontOfSize_(13))
        view.addSubview_(self._auto_delete_field)

        delete_stepper = NSStepper.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + 55, y, 19, ROW_H))
        delete_stepper.setMinValue_(1)
        delete_stepper.setMaxValue_(365)
        delete_stepper.setIncrement_(1)
        delete_stepper.setIntValue_(delete_val)
        delete_stepper.setTag_(410)
        delete_stepper.setTarget_(self._delegate)
        delete_stepper.setAction_(b'onStepper:')
        view.addSubview_(delete_stepper)

        days_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(CTRL_X + 80, y, 40, ROW_H))
        days_label.setStringValue_("days")
        days_label.setEditable_(False)
        days_label.setBordered_(False)
        days_label.setDrawsBackground_(False)
        days_label.setFont_(NSFont.systemFontOfSize_(12))
        view.addSubview_(days_label)
        y -= ROW_H + GAP

        # Anonymize Bluetooth
        anon_bt_chk = _make_checkbox("Anonymize Bluetooth", CTRL_X, y)
        anon_bt_chk.setTag_(402)
        anon_bt_chk.setState_(NSControlStateValueOn if self._get("privacy.anonymize_bluetooth", False) else NSControlStateValueOff)
        anon_bt_chk.setTarget_(self._delegate)
        anon_bt_chk.setAction_(b'onCheckbox:')
        view.addSubview_(anon_bt_chk)

        return item
