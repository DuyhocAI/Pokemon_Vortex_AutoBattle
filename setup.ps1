# Setup script cho Pokemon Vortex Agent
Write-Host "=== Pokemon Vortex Agent Setup ===" -ForegroundColor Cyan

# Tạo virtual environment
if (-not (Test-Path "venv")) {
    Write-Host "Tạo virtual environment..." -ForegroundColor Yellow
    python -m venv venv
}

# Activate venv
& "venv\Scripts\Activate.ps1"

# Cài dependencies
Write-Host "Cài đặt dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt

# Cài Playwright browsers
Write-Host "Cài đặt Playwright Chromium..." -ForegroundColor Yellow
playwright install chromium

# Tạo file .env nếu chưa có
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "File .env đã được tạo. Hãy mở file .env và điền:" -ForegroundColor Green
    Write-Host "  VORTEX_USERNAME=ten_tai_khoan_cua_ban" -ForegroundColor White
    Write-Host "  VORTEX_PASSWORD=mat_khau_cua_ban" -ForegroundColor White
} else {
    Write-Host ".env đã tồn tại, bỏ qua." -ForegroundColor Gray
}

Write-Host ""
Write-Host "Setup hoàn tất!" -ForegroundColor Green
Write-Host "Cách chạy: python main.py" -ForegroundColor Cyan
