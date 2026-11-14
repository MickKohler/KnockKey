"""
KnockKey – Klopf auf den Laptop-Rand und lös Aktionen aus (Windows).

Installation:
    pip install customtkinter sounddevice numpy pyautogui pynput pystray pillow pyperclip
Start:
    python knockkey.py          (Fenster)
    python knockkey.py --tray   (startet versteckt im Infobereich)
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque

import customtkinter as ctk
import numpy as np
import pyautogui
import sounddevice as sd
from pynput import keyboard

try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # Infobereich ist optional
    pystray = None

APP = "KnockKey"
VERSION = "1.0"
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ---------- Design ----------
BG = "#0F1117"
CARD = "#181B23"
CARD2 = "#222632"
BORDER = "#2A2F3C"
TEXT = "#E9EBF2"
MUTED = "#8B91A3"
ACCENT = "#7C5CFF"
ACCENT_HOVER = "#6948F0"
OK = "#3DDC97"
WARN = "#FFB547"
ERR = "#FF5C7A"
FONT = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
SCALE_MAX = 0.5          # Pegel-Anzeige reicht bis 0.5 (Wurzel-Skala)

ACTION_TYPES = {
    "hotkey": "Tastenkürzel",
    "open": "Programm / Datei / URL öffnen",
    "command": "Befehl ausführen",
    "text": "Text einfügen",
    "lock": "PC sperren",
}
ACTION_HINTS = {
    "hotkey": "Tasten mit + verbinden, z. B. ctrl+shift+esc – oder „Aufnehmen“ klicken.",
    "open": "z. B. notepad.exe, C:\\Users\\…\\Datei.pdf oder https://ilias.studium.kit.edu",
    "command": "Wird in der Eingabeaufforderung (cmd) ausgeführt.",
    "text": "Wird an der Cursorposition eingefügt.",
    "lock": "Kein Wert nötig.",
}
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


def F(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


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


# ---------- Aktionen ----------
def pretty_hotkey(value):
    return " + ".join(k.strip().capitalize() for k in value.split("+") if k.strip())


def describe(cmd):
    t, v = cmd["type"], cmd.get("value", "")
    if t == "hotkey":
        return f"⌨  {pretty_hotkey(v)}"
    if t == "lock":
        return "🔒  PC sperren"
    icon = {"open": "↗", "command": "›_", "text": "✎"}[t]
    short = v if len(v) <= 26 else v[:24] + "…"
    return f"{icon}  {short}"


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
            import ctypes
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
        threading.Timer(8.0, self._finish).start()   # Sicherheitsnetz

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


# ---------- UI-Bausteine ----------
def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14,
                        border_width=1, border_color=BORDER, **kw)


def section_title(parent, text):
    return ctk.CTkLabel(parent, text=text, font=F(15, "bold"), text_color=TEXT, anchor="w")


def make_icon(active=True, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=size // 4,
                        fill=ACCENT if active else "#555A66")
    r, y = size // 12, size // 2
    for x in (size * 0.3, size * 0.5, size * 0.7):
        d.ellipse((x - r, y - r, x + r, y + r), fill="white")
    return img


class CommandDialog(ctk.CTkToplevel):
    def __init__(self, app, cmd=None, on_save=None):
        super().__init__(app)
        self.app, self.on_save = app, on_save
        self.cmd = dict(cmd) if cmd else {"name": "", "knocks": 2, "type": "hotkey",
                                          "value": "", "enabled": True}
        self.title("Befehl bearbeiten" if cmd else "Neuer Befehl")
        self.configure(fg_color=BG)
        self.geometry("480x560")
        self.resizable(False, False)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)

        body = card(self)
        body.grid(row=0, column=0, padx=16, pady=16, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        row = 0

        def label(text, top=14):
            nonlocal row
            ctk.CTkLabel(body, text=text, font=F(12), text_color=MUTED, anchor="w").grid(
                row=row, column=0, padx=18, pady=(top, 4), sticky="w")
            row += 1

        label("Vorlage", 18)
        self.preset = ctk.CTkOptionMenu(
            body, values=["— eigene —"] + [p[0] for p in PRESETS], command=self._apply_preset,
            fg_color=CARD2, button_color=CARD2, button_hover_color=BORDER,
            dropdown_fg_color=CARD2, font=F(13), dropdown_font=F(13))
        self.preset.grid(row=row, column=0, padx=18, sticky="ew"); row += 1

        label("Name")
        self.name = ctk.CTkEntry(body, font=F(13), fg_color=CARD2, border_color=BORDER)
        self.name.insert(0, self.cmd["name"])
        self.name.grid(row=row, column=0, padx=18, sticky="ew"); row += 1

        label("Anzahl Klopfer")
        self.knocks = ctk.CTkSegmentedButton(
            body, values=["2", "3", "4", "5", "6"], font=F(13, "bold"),
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD2, fg_color=CARD2)
        self.knocks.set(str(self.cmd["knocks"]))
        self.knocks.grid(row=row, column=0, padx=18, sticky="ew"); row += 1

        label("Aktion")
        self.type_labels = {v: k for k, v in ACTION_TYPES.items()}
        self.type = ctk.CTkOptionMenu(
            body, values=list(ACTION_TYPES.values()), command=lambda _: self._type_changed(),
            fg_color=CARD2, button_color=CARD2, button_hover_color=BORDER,
            dropdown_fg_color=CARD2, font=F(13), dropdown_font=F(13))
        self.type.set(ACTION_TYPES[self.cmd["type"]])
        self.type.grid(row=row, column=0, padx=18, sticky="ew"); row += 1

        label("Wert")
        vrow = ctk.CTkFrame(body, fg_color="transparent")
        vrow.grid(row=row, column=0, padx=18, sticky="ew"); row += 1
        vrow.grid_columnconfigure(0, weight=1)
        self.value = ctk.CTkEntry(vrow, font=F(13), fg_color=CARD2, border_color=BORDER)
        self.value.insert(0, self.cmd.get("value", ""))
        self.value.grid(row=0, column=0, sticky="ew")
        self.rec_btn = ctk.CTkButton(vrow, text="⏺ Aufnehmen", width=120, font=F(13),
                                     fg_color=CARD2, hover_color=BORDER, command=self._record)
        self.rec_btn.grid(row=0, column=1, padx=(8, 0))
        self.hint = ctk.CTkLabel(body, text="", font=F(11), text_color=MUTED, anchor="w",
                                 justify="left", wraplength=400)
        self.hint.grid(row=row, column=0, padx=18, pady=(6, 0), sticky="w"); row += 1

        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=row, column=0, padx=18, pady=(22, 18), sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(btns, text="▶ Testen", width=100, font=F(13), fg_color=CARD2,
                      hover_color=BORDER, command=self._test).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(btns, text="Abbrechen", width=100, font=F(13), fg_color="transparent",
                      hover_color=CARD2, border_width=1, border_color=BORDER,
                      command=self.destroy).grid(row=0, column=1, padx=8)
        ctk.CTkButton(btns, text="Speichern", width=110, font=F(13, "bold"), fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=self._save).grid(row=0, column=2)

        self._type_changed()
        self.after(150, self._focus)

    def _focus(self):
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except Exception:
            pass

    def _apply_preset(self, choice):
        for name, t, v in PRESETS:
            if name == choice:
                self.type.set(ACTION_TYPES[t])
                self.value.delete(0, "end")
                self.value.insert(0, v)
                self.name.delete(0, "end")
                self.name.insert(0, name)
                self._type_changed()

    def _type_changed(self):
        t = self.type_labels[self.type.get()]
        self.hint.configure(text=ACTION_HINTS[t])
        self.value.configure(state="disabled" if t == "lock" else "normal")
        if t == "hotkey":
            self.rec_btn.grid()
        else:
            self.rec_btn.grid_remove()

    def _record(self):
        self.rec_btn.configure(text="Drück die Tasten…", fg_color=ACCENT)
        HotkeyRecorder(lambda combo: self.app.call_soon(lambda: self._recorded(combo))).start()

    def _recorded(self, combo):
        if not self.winfo_exists():
            return
        self.rec_btn.configure(text="⏺ Aufnehmen", fg_color=CARD2)
        if combo:
            self.value.delete(0, "end")
            self.value.insert(0, combo)

    def _collect(self):
        t = self.type_labels[self.type.get()]
        value = self.value.get().strip() if t != "lock" else ""
        if t != "lock" and not value:
            self.hint.configure(text="Bitte einen Wert eingeben.", text_color=ERR)
            return None
        name = self.name.get().strip() or (ACTION_TYPES[t] if t == "lock" else value)
        return {"name": name, "knocks": int(self.knocks.get()), "type": t, "value": value,
                "enabled": self.cmd.get("enabled", True)}

    def _test(self):
        cmd = self._collect()
        if cmd:
            self.hint.configure(text="Wird in 1 Sekunde ausgeführt …", text_color=MUTED)
            self.after(1000, lambda: self.app.execute(cmd, test=True))

    def _save(self):
        cmd = self._collect()
        if cmd:
            self.on_save(cmd)
            self.destroy()


# ---------- Hauptfenster ----------
class App(ctk.CTk):
    def __init__(self, start_hidden=False):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.cfg = load_config()
        self.title(APP)
        self.configure(fg_color=BG)
        self.geometry("1000x650")
        self.minsize(900, 600)

        self.audio_q = queue.Queue()
        self.ui_q = queue.Queue()
        self.engine = AudioEngine(self.audio_q)
        self.detector = Detector(self.cfg)
        self.level = 0.0
        self.hold = 0.0
        self.groups = deque(maxlen=50)     # (t, peak) – je Klopfer der Maximalwert
        self.flash_until = 0.0
        self.flash_color = OK
        self.mic_ok = False
        self.tray = None

        self._build()
        self._refresh_commands()
        self._start_audio()
        self._start_key_listener()
        self._start_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.tray is not None:
            try:
                self._icon_ref = ImageTk.PhotoImage(make_icon(True, 32))
                self.after(300, lambda: self.iconphoto(False, self._icon_ref))
            except Exception:
                pass
        self.log(f"{APP} gestartet", MUTED)
        self.after(25, self._tick)
        if start_hidden and self.tray is not None:
            self.after(0, self.withdraw)

    # ----- Aufbau -----
    def _build(self):
        self.grid_columnconfigure(0, weight=0, minsize=380)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Kopfzeile
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, padx=22, pady=(18, 10), sticky="ew")
        head.grid_columnconfigure(1, weight=1)
        logo = ctk.CTkFrame(head, fg_color="transparent")
        logo.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(logo, text="KnockKey", font=F(24, "bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(logo, text="Klopf auf den Rand – der Laptop macht den Rest.",
                     font=F(12), text_color=MUTED).pack(anchor="w")

        right = ctk.CTkFrame(head, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e")
        self.dots = ctk.CTkCanvas(right, width=118, height=22, bg=BG, highlightthickness=0)
        self.dots.grid(row=0, column=0, padx=(0, 16))
        self.status = ctk.CTkLabel(right, text="", font=F(13, "bold"))
        self.status.grid(row=0, column=1, padx=(0, 12))
        self.switch = ctk.CTkSwitch(right, text="", width=50, switch_width=46, switch_height=24,
                                    progress_color=ACCENT, command=self._toggle_enabled)
        self.switch.grid(row=0, column=2)
        if self.cfg["enabled"]:
            self.switch.select()

        # Linke Spalte
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=1, column=0, padx=(22, 10), pady=(0, 22), sticky="nsew")
        left.grid_columnconfigure(0, weight=1)

        mic = card(left)
        mic.grid(row=0, column=0, sticky="ew")
        mic.grid_columnconfigure(0, weight=1)
        section_title(mic, "Mikrofon & Empfindlichkeit").grid(
            row=0, column=0, padx=18, pady=(16, 10), sticky="w")
        self.devices = []
        self.device_menu = ctk.CTkOptionMenu(
            mic, values=["—"], command=self._device_chosen, fg_color=CARD2,
            button_color=CARD2, button_hover_color=BORDER, dropdown_fg_color=CARD2,
            font=F(12), dropdown_font=F(12), dynamic_resizing=False)
        self.device_menu.grid(row=1, column=0, padx=18, sticky="ew")

        meter_box = ctk.CTkFrame(mic, fg_color=CARD2, corner_radius=10)
        meter_box.grid(row=2, column=0, padx=18, pady=(14, 4), sticky="ew")
        self.meter = ctk.CTkCanvas(meter_box, height=34, bg=CARD2, highlightthickness=0)
        self.meter.pack(fill="x", padx=8, pady=8)
        self.slider = ctk.CTkSlider(mic, from_=0, to=1, number_of_steps=400,
                                    button_color=WARN, button_hover_color=WARN,
                                    progress_color=BORDER, fg_color=BORDER,
                                    command=self._threshold_changed)
        self.slider.set((self.cfg["threshold"] / SCALE_MAX) ** 0.5)
        self.slider.grid(row=3, column=0, padx=18, sticky="ew")
        self.meter_info = ctk.CTkLabel(mic, text="", font=F(12), text_color=MUTED, anchor="w")
        self.meter_info.grid(row=4, column=0, padx=18, pady=(6, 0), sticky="w")
        ctk.CTkButton(mic, text="Schwelle automatisch setzen", font=F(13), fg_color=CARD2,
                      hover_color=BORDER, command=self._auto_threshold).grid(
            row=5, column=0, padx=18, pady=(10, 4), sticky="ew")
        ctk.CTkLabel(mic, text="Klopf 2–3× auf den Rand und drück dann den Button.",
                     font=F(11), text_color=MUTED).grid(row=6, column=0, padx=18,
                                                       pady=(0, 16), sticky="w")

        opts = card(left)
        opts.grid(row=1, column=0, pady=(14, 0), sticky="ew")
        opts.grid_columnconfigure(0, weight=1)
        section_title(opts, "Einstellungen").grid(row=0, column=0, columnspan=2, padx=18,
                                                   pady=(16, 8), sticky="w")
        ctk.CTkLabel(opts, text="Max. Pause zwischen Klopfern", font=F(13),
                     text_color=TEXT).grid(row=1, column=0, padx=18, sticky="w")
        self.gap_label = ctk.CTkLabel(opts, text="", font=F(13), text_color=MUTED)
        self.gap_label.grid(row=1, column=1, padx=18, sticky="e")
        self.gap = ctk.CTkSlider(opts, from_=0.25, to=1.0, number_of_steps=15,
                                 button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                                 progress_color=ACCENT, command=self._gap_changed)
        self.gap.set(self.cfg["max_gap"])
        self.gap.grid(row=2, column=0, columnspan=2, padx=18, pady=(4, 10), sticky="ew")
        self._gap_changed(self.cfg["max_gap"], save=False)
        r = 3
        for key, text, cb in [
            ("typing_lockout", "Beim Tippen ignorieren", None),
            ("minimize_to_tray", "Beim Schließen in den Infobereich", None),
            ("autostart", "Mit Windows starten", self._autostart_changed),
        ]:
            sw = ctk.CTkSwitch(opts, text=text, font=F(13), text_color=TEXT,
                               progress_color=ACCENT,
                               command=lambda k=key, c=cb: self._opt_changed(k, c))
            if self.cfg[key]:
                sw.select()
            sw.grid(row=r, column=0, columnspan=2, padx=18, pady=5, sticky="w")
            setattr(self, f"sw_{key}", sw)
            r += 1
        ctk.CTkFrame(opts, fg_color="transparent", height=8).grid(row=r, column=0)

        # Rechte Spalte
        rightcol = ctk.CTkFrame(self, fg_color="transparent")
        rightcol.grid(row=1, column=1, padx=(10, 22), pady=(0, 22), sticky="nsew")
        rightcol.grid_columnconfigure(0, weight=1)
        rightcol.grid_rowconfigure(0, weight=3)
        rightcol.grid_rowconfigure(1, weight=1)

        cmds = card(rightcol)
        cmds.grid(row=0, column=0, sticky="nsew")
        cmds.grid_columnconfigure(0, weight=1)
        cmds.grid_rowconfigure(1, weight=1)
        section_title(cmds, "Befehle").grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")
        ctk.CTkButton(cmds, text="+  Neuer Befehl", width=140, font=F(13, "bold"),
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._new_command).grid(row=0, column=1, padx=18,
                                                      pady=(16, 8), sticky="e")
        self.cmd_list = ctk.CTkScrollableFrame(cmds, fg_color="transparent")
        self.cmd_list.grid(row=1, column=0, columnspan=2, padx=8, pady=(0, 10), sticky="nsew")
        self.cmd_list.grid_columnconfigure(0, weight=1)

        act = card(rightcol)
        act.grid(row=1, column=0, pady=(14, 0), sticky="nsew")
        act.grid_columnconfigure(0, weight=1)
        act.grid_rowconfigure(1, weight=1)
        section_title(act, "Aktivität").grid(row=0, column=0, padx=18, pady=(14, 6), sticky="w")
        self.logbox = ctk.CTkTextbox(act, fg_color=CARD2, font=F(12), text_color=TEXT,
                                     height=110, corner_radius=10, wrap="none")
        self.logbox.grid(row=1, column=0, padx=18, pady=(0, 16), sticky="nsew")
        for tag, color in (("muted", MUTED), ("ok", OK), ("err", ERR), ("warn", WARN)):
            self.logbox.tag_config(tag, foreground=color)
        self.logbox.configure(state="disabled")

    # ----- Befehlsliste -----
    def _refresh_commands(self):
        for w in self.cmd_list.winfo_children():
            w.destroy()
        cmds = self.cfg["commands"]
        if not cmds:
            ctk.CTkLabel(self.cmd_list, text="Noch keine Befehle – leg oben einen an.",
                         font=F(13), text_color=MUTED).grid(row=0, column=0, pady=30)
        first = {}
        for i, c in enumerate(cmds):
            if c["enabled"]:
                first.setdefault(c["knocks"], i)
        for i, c in enumerate(cmds):
            shadowed = c["enabled"] and first.get(c["knocks"]) != i
            row = ctk.CTkFrame(self.cmd_list, fg_color=CARD2, corner_radius=12)
            row.grid(row=i, column=0, padx=8, pady=5, sticky="ew")
            row.grid_columnconfigure(1, weight=1)
            badge_color = (WARN if shadowed else ACCENT) if c["enabled"] else BORDER
            ctk.CTkLabel(row, text=f'{c["knocks"]}×', width=50, height=50, corner_radius=10,
                         fg_color=badge_color, text_color="white",
                         font=F(18, "bold")).grid(row=0, column=0, rowspan=2, padx=12, pady=10)
            ctk.CTkLabel(row, text=c["name"], font=F(14, "bold"), anchor="w",
                         text_color=TEXT if c["enabled"] else MUTED).grid(
                row=0, column=1, sticky="sw", pady=(10, 0))
            sub = describe(c)
            if shadowed:
                sub = "⚠ Klopfzahl doppelt – inaktiv"
            ctk.CTkLabel(row, text=sub, font=F(12), anchor="w",
                         text_color=WARN if shadowed else MUTED).grid(
                row=1, column=1, sticky="nw", pady=(0, 10))
            tools = ctk.CTkFrame(row, fg_color="transparent")
            tools.grid(row=0, column=2, rowspan=2, padx=10)
            sw = ctk.CTkSwitch(tools, text="", width=46, progress_color=ACCENT,
                               command=lambda i=i: self._toggle_cmd(i))
            if c["enabled"]:
                sw.select()
            sw.grid(row=0, column=0, padx=(0, 6))
            for col, (txt, fn) in enumerate([("▶", lambda i=i: self.execute(self.cfg["commands"][i], test=True)),
                                             ("✎", lambda i=i: self._edit_command(i)),
                                             ("✕", lambda i=i: self._delete_command(i))], start=1):
                ctk.CTkButton(tools, text=txt, width=34, height=34, font=F(14),
                              fg_color="transparent", hover_color=BORDER,
                              text_color=ERR if txt == "✕" else TEXT,
                              command=fn).grid(row=0, column=col, padx=2)

    def _new_command(self):
        used = {c["knocks"] for c in self.cfg["commands"] if c["enabled"]}
        free = next((n for n in range(2, 7) if n not in used), 2)
        CommandDialog(self, {"name": "", "knocks": free, "type": "hotkey", "value": "",
                             "enabled": True}, on_save=self._add_command).title("Neuer Befehl")

    def _add_command(self, cmd):
        self.cfg["commands"].append(cmd)
        self._commands_changed()

    def _edit_command(self, i):
        def save(cmd):
            self.cfg["commands"][i] = cmd
            self._commands_changed()
        CommandDialog(self, self.cfg["commands"][i], on_save=save)

    def _delete_command(self, i):
        c = self.cfg["commands"].pop(i)
        self.log(f'Befehl „{c["name"]}“ gelöscht', MUTED)
        self._commands_changed()

    def _toggle_cmd(self, i):
        self.cfg["commands"][i]["enabled"] = not self.cfg["commands"][i]["enabled"]
        self._commands_changed()

    def _commands_changed(self):
        save_config(self.cfg)
        self._refresh_commands()

    # ----- Einstellungen -----
    def _toggle_enabled(self):
        self.cfg["enabled"] = bool(self.switch.get())
        self.detector.knocks = []
        save_config(self.cfg)
        self._update_status()
        if self.tray is not None:
            self.tray.icon = make_icon(self.cfg["enabled"])
            self.tray.update_menu()

    def _threshold_changed(self, p):
        self.cfg["threshold"] = round(SCALE_MAX * float(p) ** 2, 4)
        save_config(self.cfg)

    def _gap_changed(self, v, save=True):
        self.cfg["max_gap"] = round(float(v), 2)
        self.gap_label.configure(text=f'{self.cfg["max_gap"]:.2f} s')
        if save:
            save_config(self.cfg)

    def _opt_changed(self, key, cb):
        self.cfg[key] = bool(getattr(self, f"sw_{key}").get())
        save_config(self.cfg)
        if cb:
            cb()

    def _autostart_changed(self):
        try:
            set_autostart(self.cfg["autostart"])
            self.log("Autostart " + ("aktiviert" if self.cfg["autostart"] else "deaktiviert"), MUTED)
        except Exception as e:
            self.log(f"Autostart fehlgeschlagen: {e}", ERR)

    def _auto_threshold(self):
        now = time.monotonic()
        recent = sorted(p for t, p in self.groups if now - t < 10)
        if not recent:
            self.log("Erst 2–3× klopfen, dann „Schwelle automatisch setzen“ drücken.", WARN)
            return
        top = recent[-3:]
        thr = round(max(0.005, min(top) * 0.55), 4)
        self.cfg["threshold"] = thr
        self.slider.set((thr / SCALE_MAX) ** 0.5)
        save_config(self.cfg)
        self.log(f"Schwelle auf {thr:.3f} gesetzt", OK)

    # ----- Audio -----
    def _start_audio(self):
        try:
            self.devices = input_devices()
        except Exception as e:
            self.devices = []
            self.log(f"Audiogeräte nicht lesbar: {e}", ERR)
        labels = [lbl for _, lbl in self.devices] or ["Kein Mikrofon gefunden"]
        self.device_menu.configure(values=labels)
        chosen = self.cfg.get("device")
        if chosen not in labels:
            chosen = next((l for l in labels if "MME" in l and
                           any(w in l.lower() for w in ("mikrofon", "microphone", "mic"))),
                          labels[0])
        self.device_menu.set(chosen)
        self._open_device(chosen)

    def _device_chosen(self, label):
        self.cfg["device"] = label
        save_config(self.cfg)
        self._open_device(label)

    def _open_device(self, label):
        idx = next((i for i, l in self.devices if l == label), None)
        if idx is None:
            self.mic_ok = False
            self._update_status()
            return
        try:
            self.engine.start(idx)
            self.mic_ok = True
            self.log(f"Mikrofon: {label.split('  ·')[0]}", MUTED)
        except Exception as e:
            self.mic_ok = False
            self.log(f"Mikrofon-Fehler: {e} – anderes Gerät wählen.", ERR)
        self._update_status()

    def _start_key_listener(self):
        def on_press(_):
            self.detector.last_key = time.monotonic()
        try:
            keyboard.Listener(on_press=on_press, daemon=True).start()
        except Exception:
            pass

    # ----- Hauptschleife -----
    def call_soon(self, fn):
        self.ui_q.put(fn)

    def _tick(self):
        while True:
            try:
                self.ui_q.get_nowait()()
            except queue.Empty:
                break
        level = 0.0
        while True:
            try:
                t, p = self.audio_q.get_nowait()
            except queue.Empty:
                break
            level = max(level, p)
            if self.detector.is_loud(p):
                if self.groups and t - self.groups[-1][0] < Detector.DEBOUNCE:
                    gt, gp = self.groups[-1]
                    self.groups[-1] = (t, max(gp, p))
                else:
                    self.groups.append((t, p))
            if self.cfg["enabled"]:
                self.detector.feed(p, t)
        n = self.detector.poll(time.monotonic())
        if n:
            self._on_pattern(n)
        self.level = max(level, self.level * 0.82)
        self.hold = max(level, self.hold * 0.996)
        if self.state() != "withdrawn":
            self._draw_meter()
            self._draw_dots()
        self.after(25, self._tick)

    def _on_pattern(self, n):
        cmd = next((c for c in self.cfg["commands"] if c["enabled"] and c["knocks"] == n), None)
        if cmd is None:
            if n >= 2:
                self.log(f"{n}× geklopft – kein Befehl", MUTED)
            self._flash(BORDER)
            return
        self._flash(OK)
        self.execute(cmd)

    def execute(self, cmd, test=False):
        prefix = "Test" if test else f'{cmd["knocks"]}×'

        def worker():
            try:
                run_action(cmd)
                self.call_soon(lambda: self.log(f'{prefix}  →  {cmd["name"]}', OK))
            except Exception as e:
                self.call_soon(lambda: self.log(f'{prefix}  →  {cmd["name"]}: {e}', ERR))
        threading.Thread(target=worker, daemon=True).start()

    def _flash(self, color):
        self.flash_color = color
        self.flash_until = time.monotonic() + 0.45

    # ----- Zeichnen -----
    def _x(self, v, w):
        return 4 + (w - 8) * min(1.0, (max(v, 0.0) / SCALE_MAX) ** 0.5)

    def _draw_meter(self):
        c = self.meter
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20:
            return
        c.delete("all")
        thr = self.cfg["threshold"]
        c.create_rectangle(4, 4, w - 4, h - 4, fill=BG, outline="")
        color = OK if self.level >= thr else ACCENT
        c.create_rectangle(4, 4, self._x(self.level, w), h - 4, fill=color, outline="")
        hx = self._x(self.hold, w)
        c.create_line(hx, 4, hx, h - 4, fill=MUTED, width=2)
        tx = self._x(thr, w)
        c.create_line(tx, 0, tx, h, fill=WARN, width=2)
        now = time.monotonic()
        recent = [p for t, p in self.groups if now - t < 10]
        last = f"{max(recent):.3f}" if recent else "–"
        self.meter_info.configure(
            text=f"Schwelle {thr:.3f}   ·   lautester Klopfer (10 s) {last}")

    def _draw_dots(self):
        c = self.dots
        c.delete("all")
        flashing = time.monotonic() < self.flash_until
        n = len(self.detector.knocks)
        for i in range(6):
            x = 9 + i * 20
            if flashing:
                fill = self.flash_color
            else:
                fill = ACCENT if i < n else CARD2
            c.create_oval(x - 7, 4, x + 7, 18, fill=fill, outline="")

    def _update_status(self):
        if not self.mic_ok:
            self.status.configure(text="●  Kein Mikrofon", text_color=ERR)
        elif self.cfg["enabled"]:
            self.status.configure(text="●  Aktiv", text_color=OK)
        else:
            self.status.configure(text="●  Pausiert", text_color=MUTED)

    def log(self, msg, color=TEXT):
        tag = {MUTED: "muted", OK: "ok", ERR: "err", WARN: "warn"}.get(color)
        self.logbox.configure(state="normal")
        self.logbox.insert("1.0", f'{time.strftime("%H:%M:%S")}   {msg}\n', tag)
        lines = int(self.logbox.index("end-1c").split(".")[0])
        if lines > 80:
            self.logbox.delete("80.0", "end")
        self.logbox.configure(state="disabled")

    # ----- Infobereich -----
    def _start_tray(self):
        if pystray is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Öffnen", lambda: self.call_soon(self._show), default=True),
                pystray.MenuItem("Aktiv", lambda: self.call_soon(self._tray_toggle),
                                 checked=lambda _: self.cfg["enabled"]),
                pystray.MenuItem("Beenden", lambda: self.call_soon(self._quit)),
            )
            self.tray = pystray.Icon(APP, make_icon(self.cfg["enabled"]), APP, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def _tray_toggle(self):
        if self.cfg["enabled"]:
            self.switch.deselect()
        else:
            self.switch.select()
        self._toggle_enabled()

    def _show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close(self):
        if self.cfg["minimize_to_tray"] and self.tray is not None:
            self.withdraw()
            if not getattr(self, "_tray_hint_shown", False):
                self._tray_hint_shown = True
                try:
                    self.tray.notify("KnockKey läuft im Infobereich weiter.", APP)
                except Exception:
                    pass
        else:
            self._quit()

    def _quit(self):
        save_config(self.cfg)
        self.engine.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()


def main():
    app = App(start_hidden="--tray" in sys.argv)
    app._update_status()
    app.mainloop()


if __name__ == "__main__":
    main()
