@echo off
chcp 65001 >nul
title LMR Edit - Instalacao
cd /d "%~dp0"
echo.
echo  ==============================================
echo    LMR Edit - instalacao
echo  ==============================================
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo  Python nao encontrado. Instalando o Python 3.13...
    winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    echo.
    echo  Python instalado. FECHE esta janela e abra o instalar.bat de novo.
    pause
    exit /b 1
)

echo  [1/3] Criando o ambiente do app...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 goto erro

echo  [2/3] Instalando as bibliotecas (pode levar alguns minutos)...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto erro

echo  [3/3] Criando o atalho na area de trabalho...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\LMR Edit.lnk');" ^
  "$s.TargetPath='%~dp0iniciar.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0static\branding\lmr.ico';" ^
  "$s.WindowStyle=7; $s.Save()"

echo.
echo  Pronto! Abra o LMR Edit pelo atalho na area de trabalho.
echo.
pause
start "" "%~dp0iniciar.bat"
exit /b 0

:erro
echo.
echo  Algo deu errado na instalacao. Tire um print desta janela e envie ao suporte.
pause
exit /b 1
