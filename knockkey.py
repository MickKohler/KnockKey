"""
KnockKey – Klopf auf den Laptop-Rand und lös Aktionen aus (Windows).

Installation:
    pip install -r requirements.txt
Start:
    python knockkey.py          (Fenster)
    python knockkey.py --tray   (startet versteckt im Infobereich)

Die Oberfläche ist eine HTML-Seite (ui/index.html), die in einem eigenen Fenster
angezeigt wird. Dieses Skript hört zu, erkennt Klopfer und führt die Aktionen aus.
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque

import numpy as np
import pyautogui
import sounddevice as sd
import webview
from pynput import keyboard

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # Infobereich ist optional
    pystray = None

APP = "KnockKey"
VERSION = "1.0"
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Müssen zu --bg und --accent in ui/index.html passen.
BG_DARK = "#131312"
BG_LIGHT = "#F3F3F1"
ACCENT = "#FF6B35"
SCALE_MAX = 0.5          # höchste Schwelle, die die Anzeige darstellen kann

ACTION_TYPES = ("hotkey", "open", "command", "text", "lock")
PRESETS = [
    ("Screenshot (Ausschnitt)", "hotkey", "win+shift+s"),
    ("Screenshot speichern", "hotkey", "win+printscreen"),
    ("Desktop anzeigen", "hotkey", "win+d"),
    ("Play / Pause", "hotkey", "playpause"),
    ("Nächster Titel", "hotkey", "nexttrack"),
    ("Vorheriger Titel", "hotkey", "prevtrack"),
    ("Stummschalten", "hotkey", "volumemute"),
    ("Task-Manager", "hotkey", "ctrl+shift+esc"),
    ("Fenster wechseln", "hotkey", "alt+tab"),
    ("Nächster virtueller Desktop", "hotkey", "win+ctrl+right"),
    ("Explorer öffnen", "hotkey", "win+e"),
    ("PC sperren", "lock", ""),
    ("Rechner öffnen", "open", "calc.exe"),
    ("Webseite öffnen", "open", "https://"),
]
DEFAULT_CONFIG = {
    "device": None,
    "threshold": 0.04,
    "max_gap": 0.5,
    "typing_lockout": True,
    "enabled": True,
    "minimize_to_tray": True,
    "autostart": False,
    "commands": [
        {"name": "Screenshot", "knocks": 2, "type": "hotkey", "value": "win+shift+s", "enabled": True},
        {"name": "Play / Pause", "knocks": 3, "type": "hotkey", "value": "playpause", "enabled": True},
        {"name": "Desktop anzeigen", "knocks": 4, "type": "hotkey", "value": "win+d", "enabled": False},
    ],
}


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


# ---------- Konfiguration ----------
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


def set_autostart(on):
    if sys.platform != "win32":
        return
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}" --tray'
            else:
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                exe = pyw if os.path.exists(pyw) else sys.executable
                cmd = f'"{exe}" "{os.path.abspath(__file__)}" --tray'
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass


def system_dark():
    """True, wenn Windows auf dunkles App-Design gestellt ist."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


# ---------- Aktionen ----------
def run_action(cmd):
    t, v = cmd["type"], cmd.get("value", "").strip()
    if t == "hotkey":
        keys = [k.strip().lower() for k in v.split("+") if k.strip()]
        if not keys:
            raise ValueError("Kein Tastenkürzel angegeben")
        pyautogui.hotkey(*keys)
    elif t == "open":
        if hasattr(os, "startfile"):
            os.startfile(v)
        else:
            webbrowser.open(v)
    elif t == "command":
        subprocess.Popen(v, shell=True)
    elif t == "text":
        import pyperclip
        pyperclip.copy(v)
        pyautogui.hotkey("ctrl", "v")
    elif t == "lock":
        if sys.platform == "win32":
            ctypes.windll.user32.LockWorkStation()


# ---------- Tastenkürzel aufnehmen ----------
def _keymap(pairs):
    return {getattr(keyboard.Key, a): b for a, b in pairs if hasattr(keyboard.Key, a)}


MODIFIERS = _keymap([
    ("cmd", "win"), ("cmd_l", "win"), ("cmd_r", "win"),
    ("ctrl", "ctrl"), ("ctrl_l", "ctrl"), ("ctrl_r", "ctrl"),
    ("shift", "shift"), ("shift_l", "shift"), ("shift_r", "shift"),
    ("alt", "alt"), ("alt_l", "alt"), ("alt_r", "alt"), ("alt_gr", "altright"),
])
SPECIAL_KEYS = _keymap([
    ("print_screen", "printscreen"), ("media_play_pause", "playpause"),
    ("media_next", "nexttrack"), ("media_previous", "prevtrack"),
    ("media_volume_mute", "volumemute"), ("media_volume_up", "volumeup"),
    ("media_volume_down", "volumedown"), ("page_up", "pageup"),
    ("page_down", "pagedown"), ("caps_lock", "capslock"),
    ("num_lock", "numlock"), ("scroll_lock", "scrolllock"),
])
MOD_ORDER = ["ctrl", "alt", "altright", "shift", "win"]


def key_name(key):
    if isinstance(key, keyboard.Key):
        return MODIFIERS.get(key) or SPECIAL_KEYS.get(key) or key.name
    vk = getattr(key, "vk", None)
    if vk is not None and (65 <= vk <= 90 or 48 <= vk <= 57):
        return chr(vk).lower()
    ch = getattr(key, "char", None)
    return ch.lower() if ch else None


class HotkeyRecorder:
    """Nimmt eine Tastenkombination auf (Tasten werden währenddessen geschluckt)."""

    def __init__(self, on_done):
        self.on_done = on_done
        self.pressed, self.down = [], set()
        self.listener = None

    def start(self):
        self.listener = keyboard.Listener(on_press=self._press, on_release=self._release,
                                          suppress=True)
        self.listener.start()
        timer = threading.Timer(8.0, self._finish)   # Sicherheitsnetz
        timer.daemon = True
        timer.start()

    def _press(self, key):
        name = key_name(key)
        if name:
            if name not in self.pressed:
                self.pressed.append(name)
            self.down.add(name)

    def _release(self, key):
        self.down.discard(key_name(key))
        if not self.down and self.pressed:
            self._finish()
            return False

    def _finish(self):
        if self.listener is None:
            return
        listener, self.listener = self.listener, None
        listener.stop()
        mods = [m for m in MOD_ORDER if m in self.pressed]
        rest = [k for k in self.pressed if k not in MOD_ORDER]
        self.on_done("+".join(mods + rest))


# ---------- Audio + Erkennung ----------
class Detector:
    DEBOUNCE = 0.08
    NOISE_RATIO = 6.0
    TYPING_LOCKOUT = 0.35

    def __init__(self, cfg):
        self.cfg = cfg
        self.noise = 0.002
        self.knocks = []
        self.last_peak = 0.0
        self.last_key = 0.0

    def is_loud(self, peak):
        return peak >= 0.005 and peak > self.noise * self.NOISE_RATIO

    def feed(self, peak, t):
        """Gibt True zurück, wenn ein Klopfer registriert wurde."""
        typing = self.cfg["typing_lockout"] and t - self.last_key < self.TYPING_LOCKOUT
        if peak > self.cfg["threshold"] and self.is_loud(peak):
            if typing:
                return False
            fresh = t - self.last_peak > self.DEBOUNCE
            self.last_peak = t
            if fresh:
                self.knocks.append(t)
            return fresh
        self.noise = max(0.0003, 0.98 * self.noise + 0.02 * peak)
        return False

    def poll(self, now):
        if self.knocks and now - self.knocks[-1] > self.cfg["max_gap"]:
            n, self.knocks = len(self.knocks), []
            return n
        return None


def input_devices():
    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, f'{d["name"]}  ·  {apis[d["hostapi"]]["name"]}'))
    return out


class AudioEngine:
    def __init__(self, out_q):
        self.q = out_q
        self.stream = None

    def start(self, device):
        self.stop()
        info = sd.query_devices(device, "input")
        sr = int(info["default_samplerate"])
        self.stream = sd.InputStream(device=device, channels=1, samplerate=sr,
                                     blocksize=max(64, int(sr * 0.01)), callback=self._cb)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        self.q.put((time.monotonic(), float(np.max(np.abs(indata[:, 0])))))

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


# ---------- Symbol ----------
def make_icon(active=True, size=64):
    """Zwei Punkte = klopf, klopf. Vierfach gezeichnet und verkleinert, damit es glatt wird."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.24), fill="#1E1E1C")
    r, y = s * 0.115, s / 2
    for x in (s * 0.33, s * 0.67):
        d.ellipse((x - r, y - r, x + r, y + r), fill=ACCENT if active else "#77776F")
    return img.resize((size, size), Image.LANCZOS)


# ---------- Kern: läuft auch ohne Fenster ----------
class Core:
    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.audio_q = queue.Queue()
        self.engine = AudioEngine(self.audio_q)
        self.detector = Detector(self.cfg)
        self.samples = deque(maxlen=1200)   # (Nummer, Zeit, Pegel) – rund 12 Sekunden
        self.seq = 0
        self.groups = deque(maxlen=50)      # (Zeit, Pegel) – je Klopfer der Maximalwert
        self.events = deque(maxlen=120)     # Verlauf und erkannte Muster für die Oberfläche
        self.event_id = 0
        self.devices = []
        self.device = None
        self.mic_ok = False
        self.calib_since = None
        self.tray = None
        self.window = None
        self.hwnd = None
        self.wake = threading.Event()
        self.quitting = False
        self.tray_hint_shown = False

    def start(self):
        self.log(f"{APP} gestartet", "muted")
        self._start_audio()
        self._start_key_listener()
        self._start_tray()
        threading.Thread(target=self._run, daemon=True).start()

    # ----- Verlauf -----
    def _event(self, ev):
        with self.lock:
            self.event_id += 1
            ev["id"] = self.event_id
            self.events.append(ev)

    def log(self, msg, kind="info"):
        self._event({"type": "log", "time": time.strftime("%H:%M:%S"), "msg": msg, "kind": kind})

    # ----- Audio -----
    def _start_audio(self):
        try:
            self.devices = input_devices()
        except Exception as e:
            self.devices = []
            self.log(f"Audiogeräte nicht lesbar: {e}", "err")
        labels = [lbl for _, lbl in self.devices]
        if not labels:
            return
        chosen = self.cfg.get("device")
        if chosen not in labels:
            chosen = next((l for l in labels if "MME" in l and
                           any(w in l.lower() for w in ("mikrofon", "microphone", "mic"))),
                          labels[0])
        self.open_device(chosen)

    def open_device(self, label):
        self.device = label
        idx = next((i for i, l in self.devices if l == label), None)
        if idx is None:
            self.mic_ok = False
            return
        try:
            self.engine.start(idx)
            self.mic_ok = True
            self.log(f"Mikrofon: {label.split('  ·')[0]}", "muted")
        except Exception as e:
            self.mic_ok = False
            self.log(f"Mikrofon-Fehler: {e} – anderes Gerät wählen.", "err")

    def _start_key_listener(self):
        def on_press(_):
            self.detector.last_key = time.monotonic()
        try:
            keyboard.Listener(on_press=on_press, daemon=True).start()
        except Exception:
            pass

    # ----- Hauptschleife -----
    def _run(self):
        while not self.quitting:
            try:
                t, p = self.audio_q.get(timeout=0.05)
            except queue.Empty:
                t = None
            with self.lock:
                if t is not None:
                    self.seq += 1
                    self.samples.append((self.seq, t, p))
                    if self.detector.is_loud(p):
                        if self.groups and t - self.groups[-1][0] < Detector.DEBOUNCE:
                            self.groups[-1] = (t, max(self.groups[-1][1], p))
                        else:
                            self.groups.append((t, p))
                    if self.cfg["enabled"] and self.calib_since is None:
                        self.detector.feed(p, t)
                n = self.detector.poll(time.monotonic())
            if n:
                self._on_pattern(n)

    def _on_pattern(self, n):
        with self.lock:
            index = next((i for i, c in enumerate(self.cfg["commands"])
                          if c["enabled"] and c["knocks"] == n), -1)
            cmd = dict(self.cfg["commands"][index]) if index >= 0 else None
        self._event({"type": "pattern", "n": n, "index": index})
        if cmd is None:
            if n >= 2:
                self.log(f"{n}× geklopft – kein Befehl dafür", "muted")
            return
        self.execute(cmd)

    def execute(self, cmd, test=False):
        prefix = "Test" if test else f'{cmd["knocks"]}×'

        def worker():
            try:
                run_action(cmd)
                self.log(f'{prefix}  →  {cmd["name"]}', "ok")
            except Exception as e:
                self.log(f'{prefix}  →  {cmd["name"]}: {e}', "err")
        threading.Thread(target=worker, daemon=True).start()

    # ----- Einstellungen -----
    def set_enabled(self, on):
        with self.lock:
            self.cfg["enabled"] = bool(on)
            self.detector.knocks = []
        save_config(self.cfg)
        if self.tray is not None:
            self.tray.icon = make_icon(self.cfg["enabled"])
            self.tray.update_menu()

    def calibrate(self, want=3, timeout=8.0):
        """Wartet auf ein paar Klopfer und legt die Schwelle knapp darunter."""
        start = time.monotonic()
        with self.lock:
            self.detector.knocks = []
            self.calib_since = start
        try:
            while time.monotonic() - start < timeout:
                time.sleep(0.05)
                with self.lock:
                    mine = [g for g in self.groups if g[0] >= start]
                if len(mine) >= want and time.monotonic() - mine[-1][0] > 0.3:
                    break
            with self.lock:
                peaks = sorted(p for t, p in self.groups if t >= start)
        finally:
            self.calib_since = None
        if not peaks:
            return {"ok": False}
        thr = round(min(SCALE_MAX, max(0.005, min(peaks[-3:]) * 0.55)), 4)
        self.cfg["threshold"] = thr
        save_config(self.cfg)
        self.log(f"Schwelle auf {thr:.3f} gesetzt".replace(".", ","), "ok")
        return {"ok": True, "threshold": thr}

    # ----- Infobereich -----
    def _start_tray(self):
        if pystray is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Öffnen", lambda: self.show(), default=True),
                pystray.MenuItem("Aktiv", lambda: self.set_enabled(not self.cfg["enabled"]),
                                 checked=lambda _: self.cfg["enabled"]),
                pystray.MenuItem("Beenden", lambda: self.quit()),
            )
            self.tray = pystray.Icon(APP, make_icon(self.cfg["enabled"]), APP, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def stays_in_tray(self):
        return self.tray is not None and self.cfg["minimize_to_tray"]

    def tray_hint(self):
        if self.tray_hint_shown:
            return
        self.tray_hint_shown = True
        try:
            self.tray.notify("KnockKey läuft im Infobereich weiter.", APP)
        except Exception:
            pass

    # ----- Fenster -----
    def show(self):
        """Fenster nach vorn holen – oder der Hauptschleife sagen, dass sie eins öffnen soll."""
        if self.window is None:
            self.wake.set()
            return
        try:
            self.window.restore()
            if self.hwnd:
                ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(self.hwnd))
        except Exception:
            pass

    def open_window(self):
        """Öffnet das Fenster und kehrt erst zurück, wenn es geschlossen wurde."""
        dark = system_dark()
        with open(resource("ui", "index.html"), encoding="utf-8") as f:
            html = f.read()
        win = webview.create_window(APP, html=html, js_api=Api(self), width=800, height=700,
                                    min_size=(620, 560),
                                    background_color=BG_DARK if dark else BG_LIGHT)
        win.events.before_show += lambda *_: self._style_window(win, dark)
        self.window = win
        try:
            webview.start()
        finally:
            self.window = self.hwnd = None

    def _style_window(self, win, dark):
        if sys.platform != "win32":
            return
        try:
            self.hwnd = win.native.Handle.ToInt64()
            hwnd, dwm = ctypes.c_void_p(self.hwnd), ctypes.windll.dwmapi
            if dark:   # dunkle Titelleiste (Attribut 20, auf älteren Windows-10-Versionen 19)
                on = ctypes.c_int(1)
                if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4) != 0:
                    dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(on), 4)
            bg = BG_DARK if dark else BG_LIGHT   # Windows 11: Titelleiste in Fensterfarbe
            color = ctypes.c_int(int(bg[5:7] + bg[3:5] + bg[1:3], 16))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)
        except Exception:
            pass
        try:
            from System.Drawing import Icon
            path = os.path.join(CONFIG_DIR, "icon.ico")
            os.makedirs(CONFIG_DIR, exist_ok=True)
            make_icon(True, 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)])
            win.native.Icon = Icon(path)
        except Exception:
            pass

    def quit(self):
        self.quitting = True
        self.wake.set()
        win = self.window
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def shutdown(self):
        self.quitting = True
        save_config(self.cfg)
        self.engine.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass


# ---------- Brücke zur Oberfläche ----------
class Api:
    """Alles, was ui/index.html aufrufen darf (dort: pywebview.api.<name>)."""

    def __init__(self, core):
        self._core = core

    def get_state(self):
        c = self._core
        with c.lock:
            return {
                "version": VERSION,
                "config": json.loads(json.dumps(c.cfg)),
                "devices": [lbl for _, lbl in c.devices],
                "device": c.device,
                "mic_ok": c.mic_ok,
                "tray": c.tray is not None,
                "presets": [{"name": n, "type": t, "value": v} for n, t, v in PRESETS],
            }

    def poll(self, sample_cursor=0, event_cursor=0):
        """Wird ein paar Dutzend Mal pro Sekunde gerufen: neue Pegel, neue Ereignisse."""
        c = self._core
        now = time.monotonic()
        with c.lock:
            samples = []
            for seq, t, p in reversed(c.samples):
                if seq <= sample_cursor:
                    break
                samples.append((round(t, 4), round(p, 5)))
            samples.reverse()
            recent = [p for t, p in c.groups if now - t < 10]
            calib = None
            if c.calib_since is not None:
                calib = sum(1 for t, _ in c.groups if t >= c.calib_since)
            return {
                "now": now,
                "seq": c.seq,
                "samples": samples,
                "events": [e for e in c.events if e["id"] > event_cursor],
                "knocks": len(c.detector.knocks),
                "threshold": c.cfg["threshold"],
                "enabled": c.cfg["enabled"],
                "mic_ok": c.mic_ok,
                "loudest": max(recent) if recent else None,
                "calib": calib,
            }

    def set_enabled(self, on):
        self._core.set_enabled(on)

    def set_threshold(self, value, save=True):
        c = self._core
        c.cfg["threshold"] = round(min(SCALE_MAX, max(0.002, float(value))), 4)
        if save:
            save_config(c.cfg)

    def set_device(self, label):
        c = self._core
        c.cfg["device"] = label
        save_config(c.cfg)
        c.open_device(label)
        return {"mic_ok": c.mic_ok}

    def set_option(self, key, value):
        c = self._core
        if key == "max_gap":
            c.cfg[key] = round(min(1.0, max(0.25, float(value))), 2)
        elif key in ("typing_lockout", "minimize_to_tray", "autostart"):
            c.cfg[key] = bool(value)
            if key == "autostart":
                try:
                    set_autostart(c.cfg[key])
                    c.log("Autostart " + ("aktiviert" if c.cfg[key] else "deaktiviert"), "muted")
                except Exception as e:
                    c.cfg[key] = not c.cfg[key]
                    c.log(f"Autostart fehlgeschlagen: {e}", "err")
        else:
            return None
        save_config(c.cfg)
        return c.cfg[key]

    def calibrate(self):
        return self._core.calibrate()

    def record_hotkey(self):
        done, out = threading.Event(), []
        HotkeyRecorder(lambda combo: (out.append(combo), done.set())).start()
        done.wait(9)
        return out[0] if out else ""

    # ----- Befehle -----
    @staticmethod
    def _clean(cmd):
        t = cmd.get("type")
        if t not in ACTION_TYPES:
            raise ValueError("Unbekannte Aktion")
        return {"name": str(cmd.get("name", "")).strip() or t,
                "knocks": min(6, max(2, int(cmd.get("knocks", 2)))),
                "type": t,
                "value": str(cmd.get("value", "")),
                "enabled": bool(cmd.get("enabled", True))}

    def _commands_changed(self):
        c = self._core
        save_config(c.cfg)
        return json.loads(json.dumps(c.cfg["commands"]))

    def save_command(self, index, cmd):
        c = self._core
        with c.lock:
            if index is None:
                c.cfg["commands"].append(self._clean(cmd))
            else:
                c.cfg["commands"][int(index)] = self._clean(cmd)
            return self._commands_changed()

    def delete_command(self, index):
        c = self._core
        with c.lock:
            gone = c.cfg["commands"].pop(int(index))
            c.log(f'Befehl „{gone["name"]}“ gelöscht', "muted")
            return self._commands_changed()

    def toggle_command(self, index, on):
        c = self._core
        with c.lock:
            c.cfg["commands"][int(index)]["enabled"] = bool(on)
            return self._commands_changed()

    def test_command(self, cmd):
        self._core.execute(self._clean(cmd), test=True)


def main():
    core = Core()
    core.start()
    show = "--tray" not in sys.argv or core.tray is None
    try:
        while not core.quitting:
            if show:
                core.open_window()
                if core.quitting or not core.stays_in_tray():
                    break
                core.tray_hint()
            # Kein Fenster offen: warten, bis im Infobereich „Öffnen“ oder „Beenden“ kommt.
            core.wake.wait()
            core.wake.clear()
            show = True
    finally:
        core.shutdown()


if __name__ == "__main__":
    main()
