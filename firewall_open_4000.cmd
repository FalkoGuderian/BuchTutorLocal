@echo off
REM Firewall-Regel fuer LiteLLM (Port-Basis, oeffentliches Profil).
REM Als Administrator ausfuehren (Rechtsklick -> "Als Administrator ausfuehren").
REM
REM WICHTIG: litellm.exe startet ggf. als uv-python statt venv-python.
REM Darum erlauben wir hier den PORT (TCP 4000) statt einer bestimmten EXE.
netsh advfirewall firewall delete rule name="LiteLLM local" >nul 2>&1
netsh advfirewall firewall add rule name="LiteLLM local" dir=in action=allow protocol=TCP localport=4000 enable=yes profile=public
echo.
echo Regel gesetzt (TCP 4000, eingehend, Profil=Oeffentlich).
echo Test vom Handy:
echo   Browser oeffnen ->  http://192.168.x.x:4000/health/liveliness
echo   Antwort: "I'm alive!"
echo.
echo Zum Aufraeumen nach dem Test:
echo   netsh advfirewall firewall delete rule name="LiteLLM local"
pause
