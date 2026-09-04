param(
    [Parameter(Mandatory = $true)][int]$ProcessoLab,
    [Parameter(Mandatory = $true)][string]$Banco,
    [Parameter(Mandatory = $true)][string]$Bat
)

$limite = (Get-Date).AddHours(2)
$log = Join-Path $env:LOCALAPPDATA "IQOptionM5\reinicio_lab.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

while ((Get-Date) -lt $limite) {
    $consulta = 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(db.execute("select count(*) from operacoes where status=''aberta''").fetchone()[0])'
    $abertas = & python -c $consulta $Banco
    if ([int]($abertas | Select-Object -Last 1) -eq 0) {
        Add-Content -Path $log -Value "$(Get-Date -Format s) ordens encerradas; reiniciando Laboratorio EMA."
        # O PID foi conferido antes de criar este vigia. Só ele é encerrado;
        # nenhum outro bot ou processo Python é afetado.
        if (Get-Process -Id $ProcessoLab -ErrorAction SilentlyContinue) {
            Stop-Process -Id $ProcessoLab -ErrorAction Stop
            Start-Sleep -Seconds 2
        }
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", ('"{0}"' -f $Bat) `
            -WorkingDirectory (Split-Path $Bat) -WindowStyle Hidden
        exit 0
    }
    Start-Sleep -Seconds 15
}

Add-Content -Path $log -Value "$(Get-Date -Format s) prazo de 2h atingido; Lab não foi reiniciado."
exit 1
