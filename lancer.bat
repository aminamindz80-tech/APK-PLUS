@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PY=
where py >nul 2>&1 && set PY=py
if not defined PY where python >nul 2>&1 && set PY=python
if not defined PY goto nopy
echo Demarrage du serveur... ne fermez pas cette fenetre.
start "" /min cmd /c "timeout /t 3 >nul & start http://localhost:8000"
%PY% server.py
echo.
echo Le serveur s'est arrete. Lisez le message ci-dessus.
pause
goto :eof
:nopy
echo Python est introuvable sur ce poste.
echo Installez-le depuis https://www.python.org/downloads/ et cochez Add Python to PATH.
pause
