@echo off
setlocal
pushd "%~dp0"
set SERVER=root@47.103.61.115
set APP_DIR=/opt/touchlearn-fudan-Team_Stella
set ARCHIVE=touchlearn-0.3.1.tar.gz

echo ============================================
echo TouchLearn v0.3.1 deployment
echo Package: %CD%\%ARCHIVE%
echo Server:  %SERVER%
echo ============================================
echo.

echo [1/2] Upload package. Enter the root password when prompted.
scp "%ARCHIVE%" %SERVER%:/root/
if errorlevel 1 goto fail

echo.
echo [2/2] Extract and rebuild. Enter the root password again.
echo       The rebuild can take a few minutes.
ssh %SERVER% "tar -xzf /root/touchlearn-0.3.1.tar.gz -O ./deploy/install-update.sh > /root/install-update-031.sh && APP_DIR=%APP_DIR% bash /root/install-update-031.sh /root/touchlearn-0.3.1.tar.gz && sleep 8 && echo --- health --- && (curl -fsS http://127.0.0.1:8765/api/health || wget -qO- http://127.0.0.1:8765/api/health || true)"
if errorlevel 1 goto fail

echo.
echo Deployment finished. Press any key to close.
pause >nul
popd
exit /b 0

:fail
echo.
echo Deployment failed. Send a screenshot of this window to the assistant.
pause
popd
exit /b 1
