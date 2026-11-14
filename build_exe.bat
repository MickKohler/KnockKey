@echo off
REM Baut KnockKey.exe (liegt danach im Ordner dist\)
cd /d "%~dp0"
python -m pip install --upgrade pywebview sounddevice numpy pyautogui pynput pystray pillow pyperclip pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name KnockKey ^
  --add-data "ui;ui" ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 ^
  --hidden-import pystray._win32 ^
  knockkey.py
echo.
echo Fertig: dist\KnockKey.exe
pause
