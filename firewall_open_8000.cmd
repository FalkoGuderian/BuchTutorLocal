@echo off
REM Firewall-Regel fuer den lokalen HTTP-Server (Port 8000, oeffentliches Profil).
REM Als Administrator ausfuehren (Rechtsklick -> "Als Administrator ausfuehren").
REM
REM Das FritzBox-WLAN ist unter Windows als "Oeffentlich" kategorisiert,
REM darum MUSS profile=public gesetzt sein, sonst greift die Regel nicht.
netsh advfirewall firewall delete rule name="Local HTTP 8000" >nul 2>&1
netsh advfirewall firewall add rule name="Local HTTP 8000" dir=in action=allow protocol=TCP localport=8000 enable=yes profile=public
echo.
echo Regel gesetzt (TCP 8000, eingehend, Profil=Oeffentlich).
echo.
echo Server laeuft auf diesem PC:  http://192.168.x.x:8000/
echo Am Handy im selben WLAN:
echo   Browser oeffnen ->  http://192.168.x.x:8000/test_local.html
echo.
echo Zum Aufraeumen nach dem Test:
echo   netsh advfirewall firewall delete rule name="Local HTTP 8000"
pause
