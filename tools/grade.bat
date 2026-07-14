@echo off
rem Double-click launcher for the CSV grader.
cd /d "%~dp0.."
uv run python tools/grade.py
pause
