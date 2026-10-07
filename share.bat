@echo off
rem Start FlipComp and share it online through a Cloudflare Tunnel, so invited
rem partners can use it from their own phones and computers. Only people with a
rem personal link from the Team tab get in. Keep this window open (minimise it);
rem closing it stops the app and the sharing.
rem
rem To start it automatically when you sign in to Windows, run once:
rem   schtasks /create /tn "FlipComp share" /tr "\"%~dp0share.bat\"" /sc onlogon /f
cd /d "%~dp0"
python server.py --share
