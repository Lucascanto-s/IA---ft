@echo off
chcp 65001 >nul
title LMR Edit
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call "%~dp0instalar.bat"
    exit /b
)
echo  Abrindo o LMR Edit... (deixe esta janela aberta enquanto usa o app)
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest http://localhost:8501/_stcore/health -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process 'http://localhost:8501'; break}catch{Start-Sleep 1}}"
".venv\Scripts\python.exe" -m streamlit run app.py --server.headless=true --server.port=8501 --server.address=127.0.0.1 --browser.gatherUsageStats=false
