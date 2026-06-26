@echo off
REM ============================================================================
REM  run.bat - запуск AI-агента на Windows ОДНІЄЮ командою.
REM
REM    run            встановити залежності та запустити сервер
REM    run setup      лише підготувати venv + залежності
REM    run test       прогнати тести
REM
REM  Можна просто двічі клікнути цей файл у Провіднику.
REM  chcp 65001 + PYTHONUTF8=1 - проект у папці з кириличною назвою,
REM  без UTF-8 SQLite не відкриває базу.
REM ============================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Шукаємо Python: спершу лаунчер py, потім python у PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 start.py %*
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python start.py %*
    ) else (
        echo [X] Python не знайдено. Встановіть Python 3.10+ з https://python.org
        echo     і поставте галочку "Add Python to PATH" під час інсталяції.
        pause
        exit /b 1
    )
)

REM Якщо запустили подвійним кліком - не закривати вікно одразу.
if "%~1"=="" pause
