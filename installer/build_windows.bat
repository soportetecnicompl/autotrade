@echo off
rem Genera AutoTrade.exe (y el instalador, si tienes Inno Setup 6) en Windows.
rem   Uso: doble clic o  installer\build_windows.bat
setlocal
cd /d "%~dp0\.."
set "VERSION=%~1"
if "%VERSION%"=="" set "VERSION=1.0.0"

echo [1/4] Creando entorno de compilacion...
python -m venv .venv-build || goto :error
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :error

echo [2/4] Ejecutando pruebas...
set AUTOTRADE_SKIP_GUI=1
python -m unittest discover -s tests -t . || goto :error

echo [3/4] Empaquetando AutoTrade.exe...
pyinstaller --noconfirm --clean --windowed --name AutoTrade --paths . ^
  --collect-submodules telethon --collect-all MetaTrader5 ^
  --hidden-import main --hidden-import backtest app.py || goto :error

echo [4/4] Creando el instalador...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    "%ISCC%" /DAppVersion=%VERSION% installer\autotrade.iss || goto :error
    echo.
    echo Listo: dist-installer\AutoTrade-Setup-%VERSION%.exe
) else (
    echo.
    echo Inno Setup 6 no esta instalado: se omite el instalador.
    echo Descargalo en https://jrsoftware.org/isdl.php y vuelve a ejecutar este script.
    echo Mientras tanto puedes usar directamente dist\AutoTrade\AutoTrade.exe
)
exit /b 0

:error
echo.
echo ERROR: la compilacion fallo. Revisa los mensajes de arriba.
exit /b 1
