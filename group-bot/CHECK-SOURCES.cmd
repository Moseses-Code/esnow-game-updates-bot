@echo off
cd /d "%~dp0"
runtime\python.exe audit_sources.py
pause
