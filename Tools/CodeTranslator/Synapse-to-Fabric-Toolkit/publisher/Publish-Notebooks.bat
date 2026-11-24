@echo off
rem ============================================================================
rem  One-click: publish converted Fabric notebooks to a Fabric workspace or an Azure DevOps repo.
rem    * double-click            : pick the converted folder, then answer a few questions
rem    * drag a folder onto this file
rem    * command line            : Publish-Notebooks.bat "C:\path\to\notebooks_fabric_YYYYMMDDHHMM"
rem  It ALWAYS shows a dry run first and asks before changing anything.
rem  Sign-in: run 'az login' once (or set FABRIC_TOKEN / ADO_PAT). Secrets are never stored or printed.
rem ============================================================================
setlocal EnableDelayedExpansion
rem Keep the window open whatever happens, so errors are always visible.
if not "%S2F_CHILD%"=="1" (
  set "S2F_CHILD=1"
  cmd /k call "%~f0" %*
  exit /b
)
set "HERE=%~dp0"
set "IN=%~1"

if not "%IN%"=="" goto have_input
for /f "usebackq delims=" %%I in (`powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Select the timestamped converted notebooks output folder'; if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}"`) do set "IN=%%I"
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

echo.
echo  Notebooks folder: %IN%
echo.
echo  Publish to:
echo    [1] Microsoft Fabric workspace
echo    [2] Azure DevOps repo (new branch, optional pull request)
set "CH="
set /p "CH=Choose 1 or 2: "
if "%CH%"=="1" goto fabric
if "%CH%"=="2" goto ado
echo Invalid choice.
pause
exit /b 1

:fabric
set "WS="
set /p "WS=Fabric workspace ID (GUID): "
if "%WS%"=="" (echo Workspace ID is required.& pause & exit /b 1)
set "EX=skip"
set "U="
set /p "U=If a notebook with the same name exists: [S]kip (default) or [U]pdate (OVERWRITES it)? "
if /i "%U%"=="U" set "EX=update"
set "FF="
echo  Optional Fabric workspace folder, for example Migration\Notebooks.
echo  Missing folders are created only after confirmation; folder APIs are Preview.
echo  Update also moves existing notebooks into this folder. Skip leaves them unchanged.
set /p "FF=Destination folder (Enter for workspace root): "
if not defined FABRIC_TOKEN (
  call az account show >nul 2>nul
  if errorlevel 1 (
    echo You are not signed in to Azure. Opening sign-in...
    call az login
    if errorlevel 1 (echo Sign-in failed or Azure CLI is not installed. Run 'az login' manually, or set FABRIC_TOKEN.& pause & exit /b 2)
  )
)
set "ARGS=fabric "%IN%" --workspace-id %WS% --on-exists %EX%"
if defined FF set "ARGS=%ARGS% --folder "%FF%""
goto run

:ado
set "URL="
set /p "URL=Repo URL (https://dev.azure.com/<org>/<project>/_git/<repo>): "
if "%URL%"=="" (echo Repo URL is required.& pause & exit /b 1)
set "BB=main"
set /p "BB=Base branch [main]: "
if "%BB%"=="" set "BB=main"
set "FD=notebooks"
set /p "FD=Folder in the repo [notebooks]: "
if "%FD%"=="" set "FD=notebooks"
set "PR="
set /p "PR=Create a pull request after pushing? (y/N): "
set "ARGS=ado "%IN%" --repo-url "%URL%" --base-branch "%BB%" --folder "%FD%""
if /i "%PR%"=="Y" set "ARGS=%ARGS% --create-pr"
goto run

:run
set "INC="
set "NB=0"
for /f "usebackq delims=" %%C in (`%PY% -m s2f_publish --blocked-count "%IN%"`) do set "NB=%%C"
if not "%NB%"=="0" (
  echo.
  echo  %NB% notebook^(s^) above still contain TODO[SYN2FABRIC] markers ^(manual work not finished^).
  echo  If deployed they may fail or behave incorrectly in Fabric / the repo.
  set "BL="
  set /p "BL=Deploy these blocked notebooks as well? (y/N): "
  if /i "!BL!"=="Y" set "INC=--include-needs-manual"
)
set "ARGS=%ARGS% %INC%"
echo.
echo ---- DRY RUN (nothing is changed) ----
%PY% -m s2f_publish %ARGS% --dry-run
if errorlevel 3 (echo.& echo Nothing to publish.& pause & exit /b 3)
if errorlevel 1 (echo.& echo The dry run reported a problem; fix it and run again.& pause & exit /b 1)
echo.
set "GO="
set /p "GO=Proceed with the real publish? (y/N): "
if /i not "%GO%"=="Y" (echo Cancelled. Nothing was changed.& pause & exit /b 0)
echo.
%PY% -m s2f_publish %ARGS%
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Done.) else (echo Finished with exit code %RC%.)
pause
exit /b %RC%
