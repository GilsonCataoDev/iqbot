# Sobe o H1 assim que a ordem EURUSD das 11:00 expirar (12:00).
# O guard operacao_pendente_banco recusa iniciar enquanto houver ordem "aberta",
# e _recuperar_pendencias_inicio so espera 15min — por isso este atraso.
$alvo = (Get-Date).Date.AddHours(12).AddMinutes(2)
Write-Host "Aguardando ate $($alvo.ToString('HH:mm')) para subir o H1..."
while ((Get-Date) -lt $alvo) { Start-Sleep -Seconds 30 }

Set-Location "C:\Users\gilso\Documents\IQOptionM5"
Get-Content ".env.bat" | ForEach-Object {
    if ($_ -match '^\s*set\s+"?([A-Z_]+)=([^"]*)"?\s*$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
    }
}
Write-Host "Reapurando resultado da ordem pendente..."
python -u reapurar_resultados.py --tf h1

Write-Host "Iniciando H1..."
$Host.UI.RawUI.WindowTitle = "IQ Option - SCALPING H1 [PRACTICE]"
python -u rodar_iqoption_m5.py --scalping-h1-practice --confirmo
Write-Host "H1 encerrado. Pressione Enter para fechar."
[void][System.Console]::ReadLine()
