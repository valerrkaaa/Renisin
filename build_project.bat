@echo off
chcp 65001 >nul
setlocal

REM Перейти в папку, где лежит этот .bat файл
cd /d "%~dp0"

REM ==== Настройки ====
set "VENV_DIR=venv"
set "ENTRY_POINT=main.py"
set "APP_NAME=Renisin"

REM ==== Проверка точки входа ====
if not exist "%ENTRY_POINT%" (
    echo [ERROR] Не найден файл точки входа: %ENTRY_POINT%
    exit /b 1
)

set "APP_ICON=assets\icons\Renisin.ico"
if not exist "%APP_ICON%" (
    echo [ERROR] Не найдена иконка: %APP_ICON%
    exit /b 1
)

REM ==== Активация виртуального окружения ====
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [ERROR] Не найдено виртуальное окружение: %VENV_DIR%
    echo Создайте его командой: python -m venv %VENV_DIR%
    exit /b 1
)
call "%VENV_DIR%\Scripts\activate.bat"

REM ==== Проверка PyInstaller в venv ====
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [ERROR] PyInstaller не установлен в виртуальном окружении.
    echo Установите его: pip install pyinstaller
    exit /b 1
)

REM ==== Сборка ====
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "%APP_NAME%" ^
    --icon "%APP_ICON%" ^
    --add-data "assets\icons;assets\icons" ^
    --clean ^
    --noconfirm ^
    "%ENTRY_POINT%"

if errorlevel 1 (
    echo [ERROR] Сборка завершилась с ошибкой.
    exit /b 1
)

echo.
echo [OK] Готово. Файл: dist\%APP_NAME%.exe
endlocal