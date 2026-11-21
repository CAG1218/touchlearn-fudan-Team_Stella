@echo off
setlocal
pushd "%~dp0"
set SERVER=root@47.103.61.115
set ARCHIVE=touchlearn-0.3.9.tar.gz

echo ============================================
echo TouchLearn v0.3.9 deployment
echo Package: %CD%\%ARCHIVE%
echo Server:  %SERVER%
echo ============================================
echo.

if not exist "%ARCHIVE%" (
  echo Package not found: %CD%\%ARCHIVE%
  goto fail
)

echo [1/3] Upload package. Enter the root password when prompted.
scp "%ARCHIVE%" %SERVER%:/root/
if errorlevel 1 goto fail

echo.
echo [2/3] Extract the update script and rebuild on the server.
echo       Enter the root password again. The rebuild takes 3-8 minutes.
echo       deploy/.env and the touchlearn-outputs volume are backed up first.
ssh %SERVER% "tar -xzf /root/touchlearn-0.3.9.tar.gz -O ./deploy/install-update.sh > /root/install-update-039.sh && bash /root/install-update-039.sh /root/touchlearn-0.3.9.tar.gz"
if errorlevel 1 goto fail

echo.
echo [3/3] Local health check.
curl -fsS http://47.103.61.115:8765/api/health
echo.

echo Deployment finished. Expected version: 0.3.9
pause >nul
popd
exit /b 0

:fail
echo.
echo Deployment failed. Send a screenshot of this window to the assistant.
pause
popd
exit /b 1
