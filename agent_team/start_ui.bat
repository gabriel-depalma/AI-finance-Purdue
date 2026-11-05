@echo off
rem Double-click to open the Research Desk page in your browser. Close this window to stop it.
cd /d "%~dp0"
.venv\Scripts\python.exe ui.py
pause
