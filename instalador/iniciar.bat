@echo off
chcp 65001 >nul
title LMR Edit
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call "%~dp0instalar.bat"
    exit /b
)
echo  Abrindo o LMR Edit... (deixe esta janela aberta enquanto usa o app)
".venv\Scripts\python.exe" launcher.py
