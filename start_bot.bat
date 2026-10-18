@echo off
cd /d "%~dp0"
py -m pipenv run py ngrok_bot.py
pause
