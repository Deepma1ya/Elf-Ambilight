@echo off
REM Build Elf-Ambilight EXE (run from this folder)
python -m pip install -r "%~dp0requirements.txt"
pyinstaller --noconfirm --onefile --windowed --name Elf-Ambilight --icon "%~dp0icon.ico" --add-data "%~dp0icon.ico;." "%~dp0app.py"
copy /Y "%~dp0dist\Elf-Ambilight.exe" "E:\RGB PC PORT\Elf-Ambilight.exe"
echo.
echo EXE ready: dist\Elf-Ambilight.exe + E:\RGB PC PORT\Elf-Ambilight.exe
pause
