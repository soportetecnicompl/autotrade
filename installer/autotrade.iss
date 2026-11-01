; Instalador de AutoTrade (Inno Setup 6). Compilar con:
;   ISCC /DAppVersion=1.0.0 installer\autotrade.iss
; Requiere haber generado antes dist\AutoTrade con PyInstaller (ver build_windows.bat).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{53B319FA-BD80-4AD6-A68F-4DE22683F40E}
AppName=AutoTrade
AppVersion={#AppVersion}
AppPublisher=AutoTrade
DefaultDirName={autopf}\AutoTrade
DefaultGroupName=AutoTrade
DisableProgramGroupPage=yes
; Instalacion por usuario: no pide permisos de administrador
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=AutoTrade-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\AutoTrade.exe
CloseApplications=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el &escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "..\dist\AutoTrade\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\AutoTrade"; Filename: "{app}\AutoTrade.exe"
Name: "{autodesktop}\AutoTrade"; Filename: "{app}\AutoTrade.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoTrade.exe"; Description: "Abrir el asistente de AutoTrade ahora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Detener el bot si esta funcionando antes de borrar los archivos
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM AutoTrade.exe"; Flags: runhidden; RunOnceId: "StopAutoTrade"

[UninstallDelete]
; Quitar el inicio automatico. Tu configuracion y sesion de Telegram (%APPDATA%\AutoTrade) se conservan.
Type: files; Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\Startup\AutoTrade.cmd"
