@echo off
rem Add a Studio user (and optionally put them in an organization).
rem
rem   scripts\add-user.bat                                      asks for everything
rem   scripts\add-user.bat ann@example.com "Ann Lee" [acme editor]
rem
rem The password is never an argument (it would show in process lists and history): it is asked for without echo, or taken from
rem the ORCHIVIO_NEW_PASSWORD variable for automation. Roles: owner, admin, editor, viewer.
rem The database is ORCHIVIO_DATABASE_URL (or .env) like the API; ORCHIVIO_BIN overrides how `orchivio` is started.
rem Needs Windows PowerShell (for the hidden password prompt), which every supported Windows has.

setlocal DisableDelayedExpansion
if /i "%~1"=="/?" goto usage
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage

set "ORCHIVIO_U_EMAIL=%~1"
set "ORCHIVIO_U_NAME=%~2"
set "ORCHIVIO_U_ORG=%~3"
set "ORCHIVIO_U_ROLE=%~4"
set "ASKED="
if not defined ORCHIVIO_U_EMAIL set /p "ORCHIVIO_U_EMAIL=E-mail: "
if not defined ORCHIVIO_U_NAME set /p "ORCHIVIO_U_NAME=Display name: "
if "%~1%~2%~3%~4"=="" if not defined ORCHIVIO_NEW_PASSWORD (
    set /p "ORCHIVIO_U_ORG=Organization slug (empty = none): "
    if defined ORCHIVIO_U_ORG set /p "ORCHIVIO_U_ROLE=Role [viewer]: "
    if defined ORCHIVIO_U_ORG if not defined ORCHIVIO_U_ROLE set "ORCHIVIO_U_ROLE=viewer"
)

if not defined ORCHIVIO_U_EMAIL goto missing
if not defined ORCHIVIO_U_NAME goto missing
if defined ORCHIVIO_U_ORG if not defined ORCHIVIO_U_ROLE goto pair
if not defined ORCHIVIO_U_ORG if defined ORCHIVIO_U_ROLE goto pair

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0add-user.ps1"
exit /b %ERRORLEVEL%

:missing
echo error: an e-mail address and a name are required 1>&2
exit /b 2

:pair
echo error: give both an organization and a role, or neither 1>&2
exit /b 2

:usage
echo Usage: add-user.bat [email "Display name" [org-slug role]]
echo The password is asked for without echo, or taken from ORCHIVIO_NEW_PASSWORD.
echo Roles: owner, admin, editor, viewer.
exit /b 0
