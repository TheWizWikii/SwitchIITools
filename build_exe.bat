@echo off
cd /d "%~dp0"
where node >nul 2>&1 || (echo Instala Node.js primero & pause & exit /b 1)
where python >nul 2>&1 || (echo Instala Python primero & pause & exit /b 1)
cd installer
call npm ci || (pause & exit /b 1)
cd ..
python -m pip install pyserial pyinstaller || (pause & exit /b 1)
python -m PyInstaller --noconfirm --onefile --windowed --name SwitchIITools --add-data "installer;installer" switch2_tools.py || (pause & exit /b 1)
echo EXE generado en dist\SwitchIITools.exe
pause
