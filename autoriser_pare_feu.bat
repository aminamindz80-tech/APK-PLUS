@echo off
net session >nul 2>&1
if errorlevel 1 (
  echo Faites un clic droit sur ce fichier puis "Executer en tant qu'administrateur".
  pause & exit /b 1
)
netsh advfirewall firewall delete rule name="ERP Atelier" >nul 2>&1
netsh advfirewall firewall add rule name="ERP Atelier" dir=in action=allow protocol=TCP localport=8000 profile=private
echo Port 8000 autorise sur les reseaux prives.
pause
