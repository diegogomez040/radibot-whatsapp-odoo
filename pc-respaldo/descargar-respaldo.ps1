# Baja el ultimo respaldo completo de Radibombas desde el servidor y lo guarda en esta PC.
# Se ejecuta solo cada semana (ver instalar-tarea.ps1). Registro: C:\Respaldos\Radibombas\descargas.log
# Tambien se puede ejecutar a mano:  powershell -ExecutionPolicy Bypass -File C:\Respaldos\descargar-respaldo.ps1

$Servidor  = "YOUR.SERVER.IP.OR.HOSTNAME"                # edit before use
$Usuario   = "respaldos"                                  # cuenta de SOLO descarga (no puede escribir ni abrir consola)
$Clave     = Join-Path $env:USERPROFILE ".ssh\radibombas_respaldo"
$Carpeta   = "C:\Respaldos\Radibombas"
$Conservar = 8                                            # cuantos respaldos semanales guardar en esta PC

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Carpeta | Out-Null
$Log = Join-Path $Carpeta "descargas.log"

function Anotar($texto) {
    $linea = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $texto
    Add-Content -Path $Log -Value $linea
    Write-Host $linea
}

$tmp = Join-Path $Carpeta ".descarga"
try {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    New-Item -ItemType Directory -Path $tmp | Out-Null

    # 1. Descargar los 3 archivos
    $lote = Join-Path $tmp "lote.txt"
    Set-Content -Path $lote -Encoding ascii -Value @("get FECHA.txt", "get ultimo-completo.tar.gz.sha256", "get ultimo-completo.tar.gz")
    Push-Location $tmp
    try {
        & sftp -i $Clave -o BatchMode=yes -o StrictHostKeyChecking=accept-new -b $lote "$Usuario@$Servidor" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sftp fallo (codigo $LASTEXITCODE). Revisa la clave $Clave y la conexion a internet." }
    } finally { Pop-Location }

    # 2. Verificar que llego completo y sin danos
    $fecha = (Get-Content (Join-Path $tmp "FECHA.txt") -Raw).Trim()
    if ($fecha -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "FECHA.txt invalida: $fecha" }
    $archivo  = Join-Path $tmp "ultimo-completo.tar.gz"
    $esperado = ((Get-Content (Join-Path $tmp "ultimo-completo.tar.gz.sha256") -Raw).Trim() -split '\s+')[0].ToLower()
    $real     = (Get-FileHash -Algorithm SHA256 -Path $archivo).Hash.ToLower()
    if ($real -ne $esperado) { throw "La suma de verificacion NO coincide: el archivo llego danado. No se guarda." }

    # 3. Guardar con la fecha del respaldo
    $destino = Join-Path $Carpeta ("radibombas-completo-{0}.tar.gz" -f $fecha)
    if (Test-Path $destino) {
        Anotar "Ya tenia el respaldo del $fecha; no hay nada nuevo."
    } else {
        Move-Item -Path $archivo -Destination $destino
        Anotar ("OK: guardado radibombas-completo-{0}.tar.gz ({1:N1} MB), verificado." -f $fecha, ((Get-Item $destino).Length / 1MB))
    }

    # 4. Aviso si el respaldo del servidor esta atrasado (puede que el servidor este fallando)
    $dias = ((Get-Date) - [datetime]::ParseExact($fecha, "yyyy-MM-dd", $null)).Days
    if ($dias -gt 9) { Anotar "AVISO: el respaldo del servidor tiene $dias dias. Revisa que el respaldo semanal del servidor este funcionando." }

    # 5. Conservar solo los ultimos N
    Get-ChildItem -Path $Carpeta -Filter "radibombas-completo-*.tar.gz" |
        Sort-Object Name -Descending | Select-Object -Skip $Conservar |
        ForEach-Object { Remove-Item $_.FullName; Anotar "Borrado respaldo viejo: $($_.Name)" }
}
catch {
    Anotar ("ERROR: " + $_.Exception.Message)
    exit 1
}
finally {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}
