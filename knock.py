"""
knock.py – Klopfen auf den Laptop-Rand erkennen und Shortcuts ausführen (Windows).

Erkennung über das eingebaute Mikrofon: Ein Klopfen auf das Gehäuse erzeugt
einen kurzen, lauten Peak. Tastatur-Anschläge werden ignoriert.

Installation:
    pip install sounddevice numpy pyautogui pynput

Nutzung:
    python knock.py --calibrate     # Pegel anzeigen, Schwelle finden
    python knock.py                 # normal starten
    python knock.py --threshold 0.25

Standard-Muster:
    2x klopfen  -> Screenshot-Tool (Win+Shift+S)
    3x klopfen  -> Desktop anzeigen (Win+D)
"""

import argparse
import queue
import time

import numpy as np
import pyautogui
import sounddevice as sd
from pynput import keyboard

# ---------- Einstellungen ----------
SAMPLE_RATE = 44100
BLOCK = 512                 # ~12 ms pro Block
THRESHOLD = 0.20            # absoluter Mindestpegel (0..1), per --calibrate anpassen
NOISE_RATIO = 8.0           # Peak muss X-mal über dem Grundrauschen liegen
DEBOUNCE = 0.08             # s, ein Klopfen erzeugt mehrere Peaks -> zusammenfassen
MAX_GAP = 0.55              # s, max. Abstand zwischen Klopfern eines Musters
TYPING_LOCKOUT = 0.35       # s, nach Tastendruck keine Klopfer werten

ACTIONS = {
    2: ("Screenshot", lambda: pyautogui.hotkey("win", "shift", "s")),
    3: ("Desktop", lambda: pyautogui.hotkey("win", "d")),
}
# -----------------------------------

last_key_time = 0.0


def on_key(_):
    global last_key_time
    last_key_time = time.monotonic()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--device", type=int, default=None,
                    help="Nummer des Mikrofons (siehe --list)")
    ap.add_argument("--list", action="store_true", help="Audiogeräte anzeigen")
    args = ap.parse_args()

    if args.list:
        print(sd.query_devices())
        return

    q: "queue.Queue[float]" = queue.Queue()

    def callback(indata, frames, t, status):
        q.put(float(np.max(np.abs(indata[:, 0]))))

    keyboard.Listener(on_press=on_key).start()

    noise = 0.01
    knocks: list[float] = []
    last_peak = 0.0

    sr = SAMPLE_RATE
    if args.device is not None:
        sr = int(sd.query_devices(args.device)["default_samplerate"])

    with sd.InputStream(device=args.device, channels=1, samplerate=sr,
                        blocksize=BLOCK, callback=callback):
        print("Hört zu … (Strg+C zum Beenden)")
        while True:
            try:
                peak = q.get(timeout=0.05)
            except queue.Empty:
                peak = None
            now = time.monotonic()

            if peak is not None:
                if args.calibrate:
                    bar = "#" * int(min(peak, 1.0) * 60)
                    print(f"{peak:5.3f} {bar}")
                    continue

                is_knock = (peak > args.threshold
                            and peak > noise * NOISE_RATIO
                            and now - last_key_time > TYPING_LOCKOUT
                            and now - last_peak > DEBOUNCE)

                if is_knock:
                    last_peak = now
                    knocks.append(now)
                else:
                    # Grundrauschen langsam nachführen (nur ohne Klopfer)
                    noise = 0.98 * noise + 0.02 * peak

            # Muster auswerten, sobald nach dem letzten Klopfer Ruhe ist
            if knocks and now - knocks[-1] > MAX_GAP:
                n = len(knocks)
                knocks.clear()
                if n in ACTIONS:
                    name, action = ACTIONS[n]
                    print(f"{n}x geklopft -> {name}")
                    action()
                else:
                    print(f"{n}x geklopft (keine Aktion)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
