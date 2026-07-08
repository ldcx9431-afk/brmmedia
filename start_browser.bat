@echo off
cd /d "%~dp0"
set PYTHON_PATH=%cd%\win-unpacked\python\build_venv\python
set "PYTHONHOME="
set "PYTHONPATH="
set PYTHON_EXECUTABLE=%PYTHON_PATH%\python.exe
set PYTHONW_EXECUTABLE=%PYTHON_PATH%\pythonw.exe
set PYTHONEXECUTABLE=%PYTHON_PATH%\python.exe
set PYTHONWEXECUTABLE=%PYTHON_PATH%\pythonw.exe
set PYTHON_BIN_PATH=%PYTHON_PATH%\python.exe
set PYTHON_LIB_PATH=%PYTHON_PATH%\Lib\site-packages
set FFMPEG_PATH=%PYTHON_PATH%\ffmpeg\bin
set PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%PATH%
set PYTHONIOENCODING=utf-8
set HF_ENDPOINT=https://hf-mirror.com
set HF_HOME=%cd%\hf_download
set TRANSFORMERS_CACHE=%cd%\tf_download
set NO_PROXY=localhost,127.0.0.1,0.0.0.0,::1
set no_proxy=localhost,127.0.0.1,0.0.0.0,::1
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
if not exist "%PYTHON_EXECUTABLE%" ( echo [error] python not found & pause & exit /b 1 )
echo === Starting webui, please wait ~30s, then open the printed URL in browser ===
cd /d "%cd%\win-unpacked\python"
"%PYTHON_EXECUTABLE%" -u entry_yzy.py
echo === webui exited, code %errorlevel% ===
pause
