@echo off
REM One-command GKE benchmark: ingress + k6 + results + scale to 0.
REM
REM   set PMS_POSTGRES_PASSWORD=your-password
REM   scripts\run_gke_benchmark.cmd 1
REM   scripts\run_gke_benchmark.cmd all
REM   scripts\run_gke_benchmark.cmd 1 skip

setlocal
cd /d "%~dp0.."

set USE_GKE_GCLOUD_AUTH_PLUGIN=True

REM Ensure gcloud/kubectl are visible to Python subprocess (common Windows install path)
if exist "%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin" (
  set "PATH=%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin;%PATH%"
)

set NODE_COUNT=1
set EXTRA_ARGS=
if /i "%~1"=="all" set EXTRA_ARGS=--all
if /i not "%~1"=="" if /i not "%~1"=="all" if /i not "%~1"=="skip" set NODE_COUNT=%~1
if /i "%~1"=="skip" set EXTRA_ARGS=--skip-build --skip-deploy
if /i "%~2"=="skip" set EXTRA_ARGS=%EXTRA_ARGS% --skip-build --skip-deploy

py scripts\run_gke_benchmark.py --node-count %NODE_COUNT% %EXTRA_ARGS%
exit /b %ERRORLEVEL%
