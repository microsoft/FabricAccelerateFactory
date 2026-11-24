@echo off
rem ============================================================================
rem  One-click: Azure Databricks notebooks (.py / .sql)  ->  Microsoft Fabric notebooks (.ipynb)
rem    * double-click            : a folder picker opens
rem    * drag a folder onto this file
rem    * command line            : Convert-ADB-to-Fabric.bat "C:\path\to\notebooks"
rem  Output: <folder>_fabric_YYYYMMDDHHMM\ (with a suffix on collision); report opens automatically.
rem ============================================================================
setlocal
set "HERE=%~dp0"
set "IN=%~1"

if not "%IN%"=="" goto have_input
for /f "usebackq delims=" %%I in (`powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Select the folder that contains your Azure Databricks notebooks (.py / .sql)'; if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}"`) do set "IN=%%I"
:have_input
if "%IN%"=="" (
  echo No folder selected.
  pause
  exit /b 1
)
if "%IN:~-1%"=="\" set "IN=%IN:~0,-1%"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.9 or later was not found. Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)

cd /d "%HERE%"
echo Converting: %IN%
%PY% -m adb2fabric "%IN%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Done.) else (echo Finished with exit code %RC%.)
pause
exit /b %RC%
