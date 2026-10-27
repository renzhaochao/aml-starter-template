@echo off
cd /d "%~dp0"
"%~dp0aml-server.exe" %*
if errorlevel 1 (
  echo.
  echo Server failed. See logs\server.log and logs\http.log.
  pause
)
