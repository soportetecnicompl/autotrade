@echo off
rem Ejecuta el bot y lo reinicia si se cae. Ctrl+C (parada intencional) NO lo reinicia.
rem   - main.py sale con codigo 0 solo cuando tu lo detienes con Ctrl+C.
rem   - cualquier otra salida (error, desconexion) se reinicia a los 15 s.
setlocal
cd /d "%~dp0.."
title Autotrade - Telegram a MT5
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
set RESTARTS=0

:loop
set "AUTOTRADE_RESTARTS=%RESTARTS%"
echo [%date% %time%] Iniciando bot (reinicios previos: %RESTARTS%)
"%PY%" main.py
set "CODE=%errorlevel%"
if "%CODE%"=="0" (
    echo [%date% %time%] Bot detenido manualmente. No se reinicia.
    exit /b 0
)
echo [%date% %time%] El bot termino con codigo %CODE%. Reinicio en 15 segundos... ^(Ctrl+C para cancelar^)
timeout /t 15 /nobreak >nul
set /a RESTARTS+=1
goto loop
