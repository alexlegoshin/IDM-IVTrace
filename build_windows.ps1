# Сборка IVTrace в portable-приложение (Windows, onedir).
#
# Запуск из корня проекта в PowerShell:
#     .\build_windows.ps1
#
# Требуется активное окружение с зависимостями (см. requirements-dev.txt) и
# установленным pyinstaller. Скрипт использует текущий python из PATH.
# Если работаете через conda-окружение IVTrace, сначала:
#     conda activate IVTrace

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# conda + PyInstaller: часть системных DLL (libffi/_ctypes, libexpat, sqlite3,
# tcl/tk) лежит в <env>\Library\bin. Если окружение активировано (conda
# activate IVTrace), CONDA_PREFIX задан — добавляем его DLL-папки в PATH, иначе
# PyInstaller не найдёт зависимости и exe упадёт с "DLL load failed".
if ($env:CONDA_PREFIX) {
    $ep = $env:CONDA_PREFIX
    $dllDirs = @("$ep", "$ep\Library\bin", "$ep\Library\mingw-w64\bin", "$ep\Library\usr\bin", "$ep\DLLs") | Where-Object { Test-Path $_ }
    $env:PATH = ($dllDirs -join ";") + ";" + $env:PATH
    Write-Host "Добавлены DLL-папки окружения: $ep" -ForegroundColor DarkGray
} else {
    Write-Host "ВНИМАНИЕ: CONDA_PREFIX не задан. Если используете conda, сначала: conda activate IVTrace" -ForegroundColor Yellow
}

Write-Host "== Прогоняю самотесты перед сборкой ==" -ForegroundColor Cyan
python -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "Тесты не прошли — сборка отменена." -ForegroundColor Red
    exit 1
}

Write-Host "== Чищу прошлую сборку ==" -ForegroundColor Cyan
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

Write-Host "== PyInstaller ==" -ForegroundColor Cyan
pyinstaller ivtrace.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "Сборка PyInstaller завершилась с ошибкой." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Готово. Portable-приложение: dist\IVTrace\IVTrace.exe" -ForegroundColor Green
Write-Host "Копируйте папку dist\IVTrace целиком." -ForegroundColor Green
