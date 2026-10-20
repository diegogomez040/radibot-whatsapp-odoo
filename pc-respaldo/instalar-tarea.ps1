# Crea la tarea programada de Windows "Respaldo Radibombas":
# cada lunes a las 9:00 baja el ultimo respaldo del servidor. Si el PC esta apagado a esa hora,
# lo hace apenas se encienda (y haya internet). Se ejecuta UNA sola vez.
#   powershell -ExecutionPolicy Bypass -File C:\Respaldos\instalar-tarea.ps1

$script = "C:\Respaldos\descargar-respaldo.ps1"
if (-not (Test-Path $script)) {
    Write-Host "No encuentro $script. Copia ahi el archivo descargar-respaldo.ps1 y vuelve a ejecutar esto."
    exit 1
}

$accion     = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $script)
$disparador = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "09:00"
$ajustes    = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "Respaldo Radibombas" -Action $accion -Trigger $disparador -Settings $ajustes `
    -Description "Baja el respaldo semanal de Radibombas desde el servidor" -Force | Out-Null

Write-Host "Listo. Tarea 'Respaldo Radibombas' creada: lunes 9:00 (si el PC estaba apagado, se ejecuta al encender)."
Write-Host "Para probarla ahora:  schtasks /run /tn ""Respaldo Radibombas""   y mira C:\Respaldos\Radibombas\descargas.log"
