@echo off
echo ==========================================
echo Fakta Unik Clipper - Test Run
echo ==========================================
echo.

cd /d "C:\Users\lenovo-audit\test_pipeline"

echo [1] Cek Python venv...
if not exist "venv\Scripts\python.exe" (
    echo ❌ venv tidak ditemukan. Jalankan: python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt
    pause
    exit /b 1
)
echo ✅ venv OK

echo [2] Cek .env file...
if not exist ".env" (
    echo ❌ .env tidak ditemukan
    pause
    exit /b 1
)
echo ✅ .env OK

echo [3] Cek ffmpeg...
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo ⚠️ ffmpeg tidak di PATH. Install: winget install ffmpeg
) else (
    echo ✅ ffmpeg OK
)

echo [4] Cek cookies...
if exist "youtube_cookies.txt" (
    echo ✅ youtube_cookies.txt ditemukan
) else (
    echo ⚠️ youtube_cookies.txt tidak ada (akan pakai YT_COOKIES dari .env)
)

echo.
echo [5] Test run singkat (hanya import)...
call venv\Scripts\activate.bat
python -c "import content_factory; print('✅ Import OK')"
if %errorlevel% neq 0 (
    echo ❌ Import gagal
    pause
    exit /b 1
)

echo.
echo ==========================================
echo Semua cek PASS. Siap untuk Task Scheduler.
echo ==========================================
echo.
echo Langkah selanjutnya:
echo 1. Edit .env dengan API key asli
echo 2. (Optional) Export cookies YouTube ke youtube_cookies.txt
echo 3. Buka PowerShell as Administrator
echo 4. Jalankan: .\register_tasks.ps1
echo 5. Test manual: .\run_clipper.bat
echo.
pause