# Instala (o quita) el arranque automatico del bot en Windows.
#   Instalar:  clic derecho > "Ejecutar con PowerShell"   o   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Quitar:    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
#
# Crea la tarea "AutotradeBot": se lanza al iniciar sesion en Windows (con 30 s de espera)
# y abre una ventana con run_bot.bat. Se ejecuta SOLO con tu sesion abierta porque MT5 es
# una aplicacion de escritorio; por eso hay que configurar el inicio de sesion automatico
# (ver README, seccion "Arranque automatico").
param([switch]$Uninstall)

$TaskName = 'AutotradeBot'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Tarea '$TaskName' eliminada."
    return
}

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Bat        = Join-Path $PSScriptRoot 'run_bot.bat'
if (-not (Test-Path (Join-Path $ProjectDir 'config.ini'))) {
    Write-Warning "No existe config.ini en $ProjectDir. Crealo antes (copia config.example.ini)."
}

$User      = "$env:USERDOMAIN\$env:USERNAME"
$Action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$Bat`"" -WorkingDirectory $ProjectDir
$Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $User
$Trigger.Delay = 'PT30S'
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
              -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "Tarea '$TaskName' instalada. Se ejecutara al iniciar sesion como $User."
Write-Host "Probarla ahora:  Start-ScheduledTask -TaskName $TaskName"
