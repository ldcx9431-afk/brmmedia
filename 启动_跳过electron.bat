@echo off
REM ============================================================
REM yzylauncher 后端直启动脚本
REM 用于绕过 Electron 前端 exe 静默退出的问题，
REM 直接拉起 Python Gradio webui + ComfyUI，用浏览器访问。
REM ============================================================

cd /d "%~dp0"

SET PYTHON_PATH=%cd%\win-unpacked\python\build_venv\python

SET "PYTHONHOME="
SET "PYTHONPATH="
SET PYTHON_EXECUTABLE=%PYTHON_PATH%\python.exe
SET PYTHONW_EXECUTABLE=%PYTHON_PATH%\pythonw.exe
SET PYTHONEXECUTABLE=%PYTHON_PATH%\python.exe
SET PYTHONWEXECUTABLE=%PYTHON_PATH%\pythonw.exe
SET PYTHON_BIN_PATH=%PYTHON_PATH%\python.exe
SET PYTHON_LIB_PATH=%PYTHON_PATH%\Lib\site-packages

SET FFMPEG_PATH=%PYTHON_PATH%\ffmpeg\bin
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%PATH%

SET PYTHONIOENCODING=utf-8

REM Hugging Face
set HF_ENDPOINT=https://hf-mirror.com
set HF_HOME=%cd%\hf_download
set TRANSFORMERS_CACHE=%cd%\tf_download

REM ===== 关键修复：绕过系统代理，避免 Gradio localhost 自检返回 502 =====
set NO_PROXY=localhost,127.0.0.1,0.0.0.0,::1
set no_proxy=localhost,127.0.0.1,0.0.0.0,::1
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "all_proxy="

IF NOT EXIST "%PYTHON_EXECUTABLE%" (
    echo [error] Python not found: %PYTHON_EXECUTABLE%
    pause
    exit /b 1
)

echo === Starting yzylauncher webui (browser mode) ===
echo === 等待 ComfyUI 启动（约15-30秒），然后浏览器打开打印出来的 URL ===
echo.

cd /d "%cd%\win-unpacked\python"
"%PYTHON_EXECUTABLE%" -u entry_yzy.py

echo.
echo === webui 已退出 (exit code %errorlevel%) ===
pause
