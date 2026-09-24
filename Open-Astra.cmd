@echo off
powershell.exe -NoProfile -File "%~dp0Open-Astra.ps1" %*
if errorlevel 1 pause
