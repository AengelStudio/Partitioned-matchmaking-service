@echo off
REM One-command GKE benchmark: ingress + k6 + results + scale to 0.
REM
REM   scripts\run_gke_benchmark.cmd preflight
REM   scripts\run_gke_benchmark.cmd 1
REM   scripts\run_gke_benchmark.cmd 1 skip
REM   scripts\run_gke_benchmark.cmd 1 skip keep
REM   scripts\run_gke_benchmark.cmd 5 skip keep fresh

setlocal EnableExtensions
cd /d "%~dp0.."

set USE_GKE_GCLOUD_AUTH_PLUGIN=True

if exist "%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin" (
  set "PATH=%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin;%PATH%"
)

set "PY_ARGS=--node-count 1"

:next_arg
if "%~1"=="" goto launch
if /i "%~1"=="preflight" (
  set "PY_ARGS=--preflight"
  shift
  goto next_arg
)
if /i "%~1"=="all" (
  set "PY_ARGS=--all --node-count 1"
  shift
  goto next_arg
)
if /i "%~1"=="smoke" (
  set EXTRA_ARGS=%EXTRA_ARGS% --smoke
  shift
  goto parse_args
)
if /i "%~1"=="skip" (
  set "PY_ARGS=%PY_ARGS% --skip-build --skip-deploy"
  shift
  goto next_arg
)
if /i "%~1"=="fresh" (
  set "PY_ARGS=%PY_ARGS% --reset-postgres"
  shift
  goto next_arg
)
if /i "%~1"=="keep" (
  set "PY_ARGS=%PY_ARGS% --skip-teardown"
  shift
  goto next_arg
)
if /i "%~1"=="no-teardown" (
  set "PY_ARGS=%PY_ARGS% --skip-teardown"
  shift
  goto next_arg
)
if /i "%~1"=="1" set "PY_ARGS=--node-count 1" & shift & goto next_arg
if /i "%~1"=="3" set "PY_ARGS=--node-count 3" & shift & goto next_arg
if /i "%~1"=="5" set "PY_ARGS=--node-count 5" & shift & goto next_arg
if "%~1:~0,2%"=="--" (
  set "PY_ARGS=%PY_ARGS% %~1"
  shift
  goto next_arg
)
echo Unknown argument: %~1 >&2
exit /b 2

:launch
py scripts\run_gke_benchmark.py %PY_ARGS%
endlocal & exit /b %ERRORLEVEL%
