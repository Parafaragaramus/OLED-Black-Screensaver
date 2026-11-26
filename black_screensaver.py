"""
Per-Monitor Black Screensaver (System Tray Edition)
----------------------------------------------------
Blacks out each monitor independently once the mouse has been away
from it for a user-selectable number of seconds. Moving the mouse
onto a blacked-out monitor instantly restores it (works exactly like
a normal screensaver: once black, mouse movement is what wakes it).

Blanking is suppressed through two independent, toggleable signals:

  1. SCREEN MOTION (per-monitor, precise): periodically takes a tiny
     screenshot of each still-on monitor and compares it to the last
     one. If the image is changing (a video playing, an animation,
     etc.), that resets the monitor's idle timer -- exactly like fresh
     mouse activity would -- so THAT monitor alone stays awake while
     other idle monitors still blank normally.

  2. AUDIO (global safety net): if the system is playing audio (a live
     volume peak reading), ALL monitors are kept on. This exists
     because screen-capture can't see everything -- DRM video (Netflix
     and most streaming services) is usually blacked out in any
     screenshot API, and audio-only content (podcasts, music with a
     static UI) has no visual motion to detect at all. A short grace
     period (a few seconds) after the last detected sound smooths over
     brief quiet passages so it doesn't flicker during quiet dialogue.

Both signals can be toggled independently in Settings.

NOTE: an earlier version of this script also checked Windows' media-
session API (the "Now Playing" data source) via the winsdk package.
That check has been removed -- on some systems its async completion
callback can stall waiting for a Windows message pump that never
arrives while running synchronously inside this app's single UI
thread, which deadlocks the ENTIRE app (tray menu included, not just
the blanking logic). The audio-peak check covers the same practical
need without that risk.

Settings (idle timeout, thresholds, on/off toggles) are saved to a
JSON config file in %APPDATA%\\BlackScreensaver\\config.json and reloaded
automatically next time the app starts. A "Start automatically when
Windows starts" checkbox in Settings registers/unregisters the app in
the current user's registry Run key -- no separate shortcut needed.

Lives entirely in the system tray. Left-click (or right-click and
choose "Settings...") the tray icon to change settings, toggle
Start/Stop, or Quit.

Requirements:
    pip install pywin32 pystray pillow comtypes mss

Run:
    python black_screensaver.py

To build a standalone .exe, use the accompanying build.bat.
"""

import json
import os
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

import winreg

import win32api
import pystray
from PIL import Image, ImageDraw, ImageChops, ImageStat


def _make_dpi_aware():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_make_dpi_aware()

try:
    import comtypes
    from comtypes import GUID, COMMETHOD, IUnknown, CLSCTX_ALL
    from ctypes import POINTER, c_float, c_void_p
    from ctypes.wintypes import DWORD
    import pythoncom

    _CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    _IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _IID_IMMDevice = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _IID_IAudioMeterInformation = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
    _EDATAFLOW_ERENDER = 0
    _EROLE_EMULTIMEDIA = 1

    class _IAudioMeterInformation(IUnknown):
        _iid_ = _IID_IAudioMeterInformation
        _methods_ = [
            COMMETHOD([], comtypes.HRESULT, "GetPeakValue",
                      (["out"], POINTER(c_float), "pfPeak")),
        ]

    class _IMMDevice(IUnknown):
        _iid_ = _IID_IMMDevice
        _methods_ = [
            COMMETHOD([], comtypes.HRESULT, "Activate",
                      (["in"], POINTER(GUID), "iid"),
                      (["in"], DWORD, "dwClsCtx"),
                      (["in"], c_void_p, "pActivationParams"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        ]

    class _IMMDeviceEnumerator(IUnknown):
        _iid_ = _IID_IMMDeviceEnumerator
        _methods_ = [
            COMMETHOD([], comtypes.HRESULT, "EnumAudioEndpoints",
                      (["in"], DWORD, "dataFlow"),
                      (["in"], DWORD, "dwStateMask"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppDevices")),
            COMMETHOD([], comtypes.HRESULT, "GetDefaultAudioEndpoint",
                      (["in"], DWORD, "dataFlow"),
                      (["in"], DWORD, "role"),
                      (["out"], POINTER(POINTER(_IMMDevice)), "ppEndpoint")),
        ]

    AUDIO_AVAILABLE = True
    AUDIO_IMPORT_ERROR = None
except Exception as _e:
    AUDIO_AVAILABLE = False
    AUDIO_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

try:
    import mss

    SCREEN_CAPTURE_AVAILABLE = True
    SCREEN_CAPTURE_IMPORT_ERROR = None
except Exception as _e:
    SCREEN_CAPTURE_AVAILABLE = False
    SCREEN_CAPTURE_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

CHECK_INTERVAL_MS = 150
SCREEN_CHECK_EVERY_MS = 1000

DEFAULT_TIMEOUT = 30
DEFAULT_AUDIO_GUARD = True
DEFAULT_SCREEN_GUARD = True

AUDIO_PEAK_THRESHOLD = 0.02
AUDIO_GRACE_SECONDS = 6

SCREEN_DIFF_THRESHOLD = 3.0
SCREEN_THUMB_SIZE = (64, 36)

CONFIG_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "BlackScreensaver")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "timeout": DEFAULT_TIMEOUT,
    "audio_guard": DEFAULT_AUDIO_GUARD,
    "audio_threshold": AUDIO_PEAK_THRESHOLD,
    "screen_guard": DEFAULT_SCREEN_GUARD,
    "screen_diff_threshold": SCREEN_DIFF_THRESHOLD,
}

_STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_VALUE_NAME = "BlackScreensaver"

_com_initialized = False


def load_config():
    """Return saved settings merged over the defaults; never raises."""
    cfg = DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
        for key in DEFAULT_CONFIG:
            if key in saved:
                cfg[key] = saved[key]
    except Exception:
        pass
    return cfg


def save_config(cfg):
    """Return True on success, False on failure; never raises."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


def _startup_command():
    """The exact command line to register for auto-start."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script = os.path.abspath(__file__)
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{exe}" "{script}"'


def is_startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _STARTUP_VALUE_NAME)
        return True
    except Exception:
        return False


def set_startup_enabled(enabled: bool):
    """Return (True, None) on success or (False, error_message) on failure."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _STARTUP_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, _STARTUP_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _ensure_com():
    global _com_initialized
    if _com_initialized or not AUDIO_AVAILABLE:
        return
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    _com_initialized = True


def get_monitor_rects():
    rects = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        rects.append(info["Monitor"])
    return rects


def point_in_rect(pt, rect):
    x, y = pt
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom


def get_audio_peak():
    if not AUDIO_AVAILABLE:
        return None, f"Import failed: {AUDIO_IMPORT_ERROR}"
    _ensure_com()
    try:
        enumerator = comtypes.CoCreateInstance(
            _CLSID_MMDeviceEnumerator, _IMMDeviceEnumerator, CLSCTX_ALL
        )
        device = enumerator.GetDefaultAudioEndpoint(
            _EDATAFLOW_ERENDER, _EROLE_EMULTIMEDIA
        )
        interface = device.Activate(_IID_IAudioMeterInformation, CLSCTX_ALL, None)
        meter = interface.QueryInterface(_IAudioMeterInformation)
        return meter.GetPeakValue(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def capture_monitor_thumbnail(sct, rect):
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img.convert("L").resize(SCREEN_THUMB_SIZE)


def image_diff_score(img_a, img_b):
    if img_a is None or img_b is None or img_a.size != img_b.size:
        return None
    diff = ImageChops.difference(img_a, img_b)
    return ImageStat.Stat(diff).mean[0]


def make_tray_image(active: bool):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (40, 180, 90, 255) if active else (120, 120, 120, 255)
    draw.rounded_rectangle((6, 10, size - 6, size - 22), radius=6, fill=color)
    draw.rectangle((size // 2 - 10, size - 20, size // 2 + 10, size - 12), fill=color)
    draw.rectangle((size // 2 - 16, size - 12, size // 2 + 16, size - 6), fill=color)
    return img


class MonitorOverlay:
    def __init__(self, root, rect):
        self.rect = rect
        left, top, right, bottom = rect
        width, height = right - left, bottom - top

        self.win = tk.Toplevel(root)
        self.win.configure(bg="black")
        self.win.overrideredirect(True)
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.visible = False

    def show(self):
        if not self.visible:
            self.win.deiconify()
            self.win.lift()
            self.win.attributes("-topmost", True)
            self.visible = True

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False

    def destroy(self):
        self.win.destroy()


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()

        cfg = load_config()
        self.timeout = cfg["timeout"]
        self.audio_guard_var = tk.BooleanVar(value=cfg["audio_guard"])
        self.audio_threshold = cfg["audio_threshold"]
        self.screen_guard_var = tk.BooleanVar(value=cfg["screen_guard"])
        self.screen_diff_threshold = cfg["screen_diff_threshold"]
        # Reflects the real registry state, not the config file, so it
        # stays accurate even if changed outside this app.
        self.start_at_login_var = tk.BooleanVar(value=is_startup_enabled())

        self.running = False
        self.overlays = []
        self.monitor_rects = []
        self.last_active = []

        self.last_audio_time = 0.0
        self.last_audio_peak = None
        self.last_audio_error = None

        self.monitor_thumbnails = []
        self.last_screen_diff = []
        self._last_screen_check = 0.0
        self.last_screen_error = None

        self.sct = None
        if SCREEN_CAPTURE_AVAILABLE:
            try:
                self.sct = mss.mss()
            except Exception as e:
                self.sct = None
                self.last_screen_error = f"{type(e).__name__}: {e}"
        else:
            self.last_screen_error = f"Import failed: {SCREEN_CAPTURE_IMPORT_ERROR}"

        self.settings_win = None

        self.tray_icon = pystray.Icon(
            "black_screensaver",
            make_tray_image(False),
            "Black Screensaver",
            menu=self.build_menu(),
        )

    def build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Settings...", self.on_settings, default=True),
            pystray.MenuItem(
                lambda item: "Stop" if self.running else "Start",
                self.on_toggle,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.on_quit),
        )

    def refresh_tray(self):
        self.tray_icon.icon = make_tray_image(self.running)
        state = f"running, {int(self.timeout)}s" if self.running else "stopped"
        self.tray_icon.title = f"Black Screensaver ({state})"
        self.tray_icon.update_menu()

    def on_settings(self, icon=None, item=None):
        self.root.after(0, self.open_settings)

    def on_toggle(self, icon=None, item=None):
        self.root.after(0, self.toggle)

    def on_quit(self, icon=None, item=None):
        self.root.after(0, self.quit_app)

    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("Black Screensaver Settings")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self.settings_win = win

        pad = {"padx": 10, "pady": 5}

        ttk.Label(win, text="Idle seconds before blacking a monitor:").grid(
            row=0, column=0, columnspan=2, sticky="w", **pad
        )
        timeout_var = tk.StringVar(value=str(int(self.timeout)))
        ttk.Entry(win, textvariable=timeout_var, width=8).grid(
            row=1, column=0, sticky="w", **pad
        )

        ttk.Separator(win, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )

        screen_check_text = "Keep a monitor on while its screen is visibly changing (video motion)"
        if not SCREEN_CAPTURE_AVAILABLE:
            screen_check_text += "  (unavailable -- see error below)"
        screen_check = ttk.Checkbutton(
            win, text=screen_check_text, variable=self.screen_guard_var
        )
        screen_check.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        if not SCREEN_CAPTURE_AVAILABLE:
            screen_check.state(["disabled"])

        ttk.Label(win, text="Screen-change sensitivity (lower = more sensitive):").grid(
            row=4, column=0, columnspan=2, sticky="w", **pad
        )
        screen_threshold_var = tk.StringVar(value=f"{self.screen_diff_threshold:.1f}")
        screen_threshold_entry = ttk.Entry(win, textvariable=screen_threshold_var, width=8)
        screen_threshold_entry.grid(row=5, column=0, sticky="w", **pad)
        if not SCREEN_CAPTURE_AVAILABLE:
            screen_threshold_entry.configure(state="disabled")

        ttk.Separator(win, orient="horizontal").grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )

        audio_check_text = "Don't blank ANY monitor while audio is playing (global fallback)"
        if not AUDIO_AVAILABLE:
            audio_check_text += "  (unavailable -- see error below)"
        audio_check = ttk.Checkbutton(
            win, text=audio_check_text, variable=self.audio_guard_var
        )
        audio_check.grid(row=7, column=0, columnspan=2, sticky="w", **pad)
        if not AUDIO_AVAILABLE:
            audio_check.state(["disabled"])

        ttk.Label(win, text="Audio sensitivity threshold (lower = more sensitive):").grid(
            row=8, column=0, columnspan=2, sticky="w", **pad
        )
        threshold_var = tk.StringVar(value=f"{self.audio_threshold:.3f}")
        threshold_entry = ttk.Entry(win, textvariable=threshold_var, width=8)
        threshold_entry.grid(row=9, column=0, sticky="w", **pad)
        if not AUDIO_AVAILABLE:
            threshold_entry.configure(state="disabled")

        ttk.Separator(win, orient="horizontal").grid(
            row=10, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )

        startup_check = ttk.Checkbutton(
            win, text="Start automatically when Windows starts",
            variable=self.start_at_login_var,
        )
        startup_check.grid(row=11, column=0, columnspan=2, sticky="w", **pad)

        ttk.Separator(win, orient="horizontal").grid(
            row=12, column=0, columnspan=2, sticky="ew", padx=10, pady=4
        )

        status_var = tk.StringVar(value=f"Currently {'running' if self.running else 'stopped'}")
        ttk.Label(win, textvariable=status_var, foreground="gray").grid(
            row=13, column=0, columnspan=2, sticky="w", **pad
        )

        screen_diag_var = tk.StringVar(value="Screen motion: --")
        ttk.Label(win, textvariable=screen_diag_var, foreground="gray", wraplength=380, justify="left").grid(
            row=14, column=0, columnspan=2, sticky="w", **pad
        )

        audio_diag_var = tk.StringVar(value="Audio level: --")
        ttk.Label(win, textvariable=audio_diag_var, foreground="gray").grid(
            row=15, column=0, columnspan=2, sticky="w", **pad
        )

        def update_diag():
            if not (self.settings_win and self.settings_win.winfo_exists()):
                return

            if self.last_screen_error:
                screen_diag_var.set(f"Screen motion error: {self.last_screen_error}")
            elif self.running and self.last_screen_diff:
                parts = []
                for i, score in enumerate(self.last_screen_diff):
                    if self.overlays[i].visible:
                        parts.append(f"M{i + 1}: blanked")
                    elif score is None:
                        parts.append(f"M{i + 1}: --")
                    else:
                        changing = score > self.screen_diff_threshold
                        parts.append(f"M{i + 1}: {score:.1f}{' (changing)' if changing else ''}")
                screen_diag_var.set("Screen motion: " + "   ".join(parts))
            else:
                screen_diag_var.set("Screen motion: -- (start the screensaver to see live readings)")

            if self.last_audio_error:
                audio_diag_var.set(f"Audio detection error: {self.last_audio_error}")
            elif self.last_audio_peak is not None:
                playing = self.last_audio_peak > self.audio_threshold
                audio_diag_var.set(
                    f"Audio level: {self.last_audio_peak:.3f}  "
                    f"(threshold {self.audio_threshold:.3f} -- "
                    f"{'sound detected' if playing else 'quiet'})"
                )
            else:
                audio_diag_var.set("Audio level: -- (start the screensaver to see live readings)")

            win.after(500, update_diag)

        update_diag()

        toggle_text = tk.StringVar(value="Stop" if self.running else "Start")

        def apply_settings():
            try:
                val = float(timeout_var.get())
                if val <= 0:
                    raise ValueError
            except ValueError:
                status_var.set("Enter a positive number of seconds")
                return

            new_audio_threshold = self.audio_threshold
            if AUDIO_AVAILABLE:
                try:
                    new_audio_threshold = float(threshold_var.get())
                    if new_audio_threshold < 0:
                        raise ValueError
                except ValueError:
                    status_var.set("Enter a non-negative audio threshold")
                    return

            new_screen_threshold = self.screen_diff_threshold
            if SCREEN_CAPTURE_AVAILABLE:
                try:
                    new_screen_threshold = float(screen_threshold_var.get())
                    if new_screen_threshold < 0:
                        raise ValueError
                except ValueError:
                    status_var.set("Enter a non-negative screen-change threshold")
                    return

            self.timeout = val
            self.audio_threshold = new_audio_threshold
            self.screen_diff_threshold = new_screen_threshold

            startup_ok, startup_err = set_startup_enabled(self.start_at_login_var.get())
            if not startup_ok:
                # Revert the checkbox to reflect what's actually registered,
                # and let the user know why it didn't take.
                self.start_at_login_var.set(is_startup_enabled())
                status_var.set(f"Saved settings, but startup option failed: {startup_err}")
            else:
                saved_ok = save_config({
                    "timeout": self.timeout,
                    "audio_guard": self.audio_guard_var.get(),
                    "audio_threshold": self.audio_threshold,
                    "screen_guard": self.screen_guard_var.get(),
                    "screen_diff_threshold": self.screen_diff_threshold,
                })
                if saved_ok:
                    status_var.set(f"Saved. Currently {'running' if self.running else 'stopped'}")
                else:
                    status_var.set("Applied, but couldn't write settings to disk")

            if self.running:
                self.stop()
                self.start()
            toggle_text.set("Stop" if self.running else "Start")
            self.refresh_tray()

        def do_toggle():
            self.toggle()
            toggle_text.set("Stop" if self.running else "Start")
            status_var.set(f"Currently {'running' if self.running else 'stopped'}")

        ttk.Button(win, text="Save", command=apply_settings).grid(
            row=9, column=1, sticky="e", **pad
        )
        ttk.Button(win, textvariable=toggle_text, command=do_toggle).grid(
            row=16, column=0, sticky="w", **pad
        )
        ttk.Button(win, text="Close", command=win.withdraw).grid(
            row=16, column=1, sticky="e", **pad
        )
        win.protocol("WM_DELETE_WINDOW", win.withdraw)

    def start(self):
        self.monitor_rects = get_monitor_rects()
        self.overlays = [MonitorOverlay(self.root, r) for r in self.monitor_rects]
        now = time.time()
        self.last_active = [now for _ in self.monitor_rects]
        self.last_audio_time = 0.0
        self.monitor_thumbnails = [None for _ in self.monitor_rects]
        self.last_screen_diff = [None for _ in self.monitor_rects]
        self._last_screen_check = 0.0
        self.running = True
        self.refresh_tray()
        self.tick()

    def stop(self):
        self.running = False
        for ov in self.overlays:
            ov.destroy()
        self.overlays = []
        self.refresh_tray()

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def tick(self):
        if not self.running:
            return
        try:
            self._tick_body()
        except Exception:
            # Never let an unexpected error kill the recurring loop --
            # log it and keep going, so the tray/Settings stay usable
            # even if one check fails.
            try:
                with open(os.path.join(_log_dir(), "black_screensaver_tick_error.log"), "a") as f:
                    f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                    f.write(traceback.format_exc())
                    f.write("\n")
            except Exception:
                pass
        self.root.after(CHECK_INTERVAL_MS, self.tick)

    def _tick_body(self):
        pos = win32api.GetCursorPos()
        now = time.time()

        peak, err = get_audio_peak()
        self.last_audio_peak = peak
        self.last_audio_error = err

        audio_guard_on = self.audio_guard_var.get()
        if audio_guard_on:
            audio_spike = (peak is not None) and (peak > self.audio_threshold)
            if audio_spike:
                self.last_audio_time = now
        global_grace_active = audio_guard_on and ((now - self.last_audio_time) < AUDIO_GRACE_SECONDS)

        screen_guard_on = self.screen_guard_var.get()
        run_screen_check = (
            SCREEN_CAPTURE_AVAILABLE and screen_guard_on and self.sct is not None
            and (now - self._last_screen_check) >= (SCREEN_CHECK_EVERY_MS / 1000.0)
        )
        if run_screen_check:
            self._last_screen_check = now
            for i, rect in enumerate(self.monitor_rects):
                if self.overlays[i].visible:
                    self.monitor_thumbnails[i] = None
                    self.last_screen_diff[i] = None
                    continue
                try:
                    thumb = capture_monitor_thumbnail(self.sct, rect)
                    prev = self.monitor_thumbnails[i]
                    score = image_diff_score(prev, thumb)
                    self.monitor_thumbnails[i] = thumb
                    self.last_screen_diff[i] = score
                    self.last_screen_error = None
                    if score is not None and score > self.screen_diff_threshold:
                        self.last_active[i] = now
                except Exception as e:
                    self.last_screen_error = f"{type(e).__name__}: {e}"

        for i, rect in enumerate(self.monitor_rects):
            if point_in_rect(pos, rect):
                self.last_active[i] = now
                self.overlays[i].hide()
            elif now - self.last_active[i] < self.timeout:
                self.overlays[i].hide()
            elif global_grace_active:
                self.overlays[i].hide()
            else:
                self.overlays[i].show()

    def quit_app(self):
        self.stop()
        if self.sct is not None:
            try:
                self.sct.close()
            except Exception:
                pass
        self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()

    def run(self):
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.start()
        self.root.mainloop()


def _log_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def main():
    try:
        App().run()
    except Exception:
        tb = traceback.format_exc()
        try:
            with open(os.path.join(_log_dir(), "black_screensaver_error.log"), "w") as f:
                f.write(tb)
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, tb, "Black Screensaver - Startup Error", 0x10
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
