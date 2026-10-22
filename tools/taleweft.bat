@echo off
rem TaleWeft entry point; delegate to the compatibility bootstrap.
call "%~dp0plotpilot.bat" %*
exit /b %errorlevel%
