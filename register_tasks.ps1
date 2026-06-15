<# 
Fakta Unik Clipper - Scheduled Task Registration
Jalankan sebagai Administrator (Run as Administrator)
#>

$projectPath = "C:\Users\lenovo-audit\test_pipeline"
$scriptPath = "$projectPath\run_clipper.bat"
$taskName = "FaktaUnikClipper"

# Pastikan folder logs ada
if (-not (Test-Path "$projectPath\logs")) {
    New-Item -ItemType Directory -Path "$projectPath\logs" | Out-Null
}

# Action: jalankan batch file
$action = New-ScheduledTaskAction `
    -Execute $scriptPath `
    -WorkingDirectory $projectPath

# Trigger 1: 06:30 WIB
$trigger1 = New-ScheduledTaskTrigger -Daily -At 06:30

# Trigger 2: 11:30 WIB
$trigger2 = New-ScheduledTaskTrigger -Daily -At 11:30

# Trigger 3: 18:30 WIB
$trigger3 = New-ScheduledTaskTrigger -Daily -At 18:30

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Principal: Run with highest privileges
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Register task (3 triggers in 1 task)
try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Description "MLBB Clipper - 3x daily (06:30, 11:30, 18:30 WIB)" `
        -Action $action `
        -Trigger $trigger1, $trigger2, $trigger3 `
        -Settings $settings `
        -Principal $principal `
        -Force `
        -ErrorAction Stop
    
    Write-Host "✅ Task '$taskName' berhasil didaftarkan!" -ForegroundColor Green
    Write-Host "   Triggers: 06:30, 11:30, 18:30 daily" -ForegroundColor Cyan
    Write-Host "   Script: $scriptPath" -ForegroundColor Cyan
}
catch {
    Write-Host "❌ Gagal daftar task: $_" -ForegroundColor Red
    exit 1
}

# Tampilkan task yang terdaftar
Get-ScheduledTask -TaskName $taskName | Format-List TaskName, State, Triggers, Actions