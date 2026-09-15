@echo off
python "%~dp0submit.py" %*
exit /b %errorlevel%
