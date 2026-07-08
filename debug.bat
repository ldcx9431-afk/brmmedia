@echo off

SET PYTHON_PATH=%cd%\win-unpacked\python\build_venv\python

SET "PYTHONHOME="
SET "PYTHONPATH="
SET PYTHON_EXECUTABLE=%PYTHON_PATH%\python.exe
SET PYTHONW_EXECUTABLE=%PYTHON_PATH%\pythonw.exe

SET PYTHONEXECUTABLE=%PYTHON_PATH%\python.exe
SET PYTHONWEXECUTABLE=%PYTHON_PATH%\pythonw.exe
SET PYTHON_BIN_PATH=%PYTHON_EXECUTABLE%
SET PYTHON_LIB_PATH=%PYTHON_PATH%\Lib\site-packages

:: SYSTEM PATH
SET FFMPEG_PATH=%PYTHON_PATH%\ffmpeg\bin
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%PATH%

:: Hugging Face
set HF_ENDPOINT=https://hf-mirror.com
set HF_HOME=%cd%\hf_download
set TRANSFORMERS_CACHE=%cd%\tf_download

:: 绕过系统代理：避免 Gradio/ComfyUI 对 localhost 的自检被代理拦截返回 502
set NO_PROXY=localhost,127.0.0.1,0.0.0.0,::1
set no_proxy=localhost,127.0.0.1,0.0.0.0,::1
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="

:: cuda nvidia-smi device select
:: set CUDA_VISIBLE_DEVICES=0,1

:: python module search path
:: set PYTHONPATH=third_party/path1;third_party/path2

IF NOT EXIST "%PYTHON_EXECUTABLE%" (
    echo "[error] Python not found: %PYTHON_EXECUTABLE%"
    pause; exit /b 1
)

cd /d "%~dp0"
echo === Running launch.ps1 ===
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
echo.
echo === Exit code: %errorlevel% ===
pause