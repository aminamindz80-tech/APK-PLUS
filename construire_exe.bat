@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PY=
where py >nul 2>&1 && set PY=py
if not defined PY where python >nul 2>&1 && set PY=python
if not defined PY (
  echo Python est introuvable. Installez-le depuis https://www.python.org/downloads/ ^(cochez Add Python to PATH^).
  pause & exit /b 1
)
echo === Installation des outils de construction ===
%PY% -m pip install --upgrade pip pyinstaller pywebview || goto err
echo === Construction de ERP_ATELIER.exe ===
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name ERP_ATELIER ^
  --add-data "static;static" ^
  --collect-all webview ^
  app_desktop.py || goto err
copy /y reinitialiser_mdp.bat dist\ERP_ATELIER\ >nul
copy /y ouvrir_dans_navigateur.bat dist\ERP_ATELIER\ >nul
copy /y acces_reseau.bat dist\ERP_ATELIER\ >nul
copy /y autoriser_pare_feu.bat dist\ERP_ATELIER\ >nul
echo.
echo Termine : dist\ERP_ATELIER\ERP_ATELIER.exe
echo Pour creer l'installateur, ouvrez installateur.iss avec Inno Setup ^(gratuit^) et cliquez sur Compile.
pause & exit /b 0
:err
echo ERREUR pendant la construction. Lisez le message ci-dessus.
pause & exit /b 1
