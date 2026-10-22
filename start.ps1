# Запуск бэкенда Алисы: HTTPS 443 + HTTP 80 (мок активации, quasar, oauth, uniproxy с GigaAM/Qwen/Silero).
#   .\start.ps1                 — боевой режим (порты из config.toml)
#   .\start.ps1 -TestPort 8443  — тест на другом порту HTTPS, 80/443 не трогаются
param([int]$TestPort = 0)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

if ($TestPort) {
    $env:ALICE_SERVER_HTTPS_PORT = "$TestPort"
    $env:ALICE_SERVER_HTTP_PORT = "0"
} else {
    # 80/443 нужны целиком: если их держит другой процесс (например, yandex_emulator.py) — сообщаем
    $busy = Get-NetTCPConnection -State Listen -LocalPort 80, 443 -ErrorAction SilentlyContinue
    foreach ($c in $busy) {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)").CommandLine
        Write-Host "Порт $($c.LocalPort) занят: pid=$($c.OwningProcess) $cmd" -ForegroundColor Red
    }
    if ($busy) { Write-Host "Останови этот процесс и запусти снова." -ForegroundColor Yellow; exit 1 }
}

# Ключ LLM: config.toml -> GROQ_API_KEY / ALICE_LLM_API_KEY -> файл groq_key.txt (если нет — открываем Блокнот)
$cfgKey = Select-String -Path "$PSScriptRoot\config.toml" -Pattern '^\s*api_key\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue
$keyFile = "$PSScriptRoot\groq_key.txt"
function Read-KeyFile { if (Test-Path $keyFile) { ((Get-Content $keyFile -Raw) -replace '\s', '') } else { "" } }
if (-not $cfgKey -and -not $env:GROQ_API_KEY -and -not $env:ALICE_LLM_API_KEY -and -not $env:ALICE_LLM_BASE_URL) {
    if (-not (Read-KeyFile)) {
        Write-Host "Нет ключа Groq. Вставь его в открывшийся Блокнот, сохрани (Ctrl+S) и закрой." -ForegroundColor Yellow
        if (-not (Test-Path $keyFile)) { New-Item -ItemType File $keyFile | Out-Null }
        Start-Process notepad.exe $keyFile -Wait
    }
    $env:GROQ_API_KEY = Read-KeyFile
    if (-not $env:GROQ_API_KEY) { Write-Host "Ключа нет — LLM отвечать не будет." -ForegroundColor Red }
}

# TTS F5 (клон голоса) — отдельный процесс в своём окружении; поднимаем, если выбран в config.toml
if (Select-String -Path "$PSScriptRoot\config.toml" -Pattern '^\s*engine\s*=\s*"f5"' -Quiet) {
    $f5ok = $false
    try { $f5ok = (Invoke-RestMethod http://127.0.0.1:8777/health -TimeoutSec 2).ok } catch {}
    if (-not $f5ok) {
        Write-Host "Запускаю F5-TTS (свёрнутое окно), загрузка ~15-30 с..." -ForegroundColor Cyan
        Start-Process -WindowStyle Minimized -WorkingDirectory $PSScriptRoot `
            -FilePath "$PSScriptRoot\tts_bench\env_f5\Scripts\python.exe" `
            -ArgumentList "tools\f5_server.py", "--ref", "voice\ref_voice.wav", "--ref-text", "voice\ref_voice.txt"
        for ($i = 0; $i -lt 120 -and -not $f5ok; $i++) {
            Start-Sleep 1
            try { $f5ok = (Invoke-RestMethod http://127.0.0.1:8777/health -TimeoutSec 2).ok } catch {}
        }
    }
    if ($f5ok) { Write-Host "F5-TTS готов" -ForegroundColor Green }
    else { Write-Host "F5-TTS не поднялся — смотри его окно" -ForegroundColor Red }
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" }).IPAddress -join ", "
Write-Host "IP этого ПК: $ip  (на него должен указывать dnsmasq на роутере)" -ForegroundColor Cyan
& "$PSScriptRoot\.venv\Scripts\python.exe" -m alice_server
