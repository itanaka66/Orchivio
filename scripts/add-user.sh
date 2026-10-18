#!/usr/bin/env sh
# Add a Studio user (and optionally put them in an organization).
#
#   scripts/add-user.sh                                   # asks for everything
#   scripts/add-user.sh -e ann@example.com -n "Ann Lee" [-o acme -r editor]
#   printf '%s\n' "$PASSWORD" | scripts/add-user.sh -e ann@example.com -n "Ann Lee" -p    # password from stdin (automation)
#
# The password is never an argument (it would show in process lists and shell history): it is asked for without echo, or read
# from stdin with -p, or taken from $ORCHIVIO_NEW_PASSWORD. Roles: owner, admin, editor, viewer.
# The database is ORCHIVIO_DATABASE_URL (or .env) like the API; $ORCHIVIO_BIN overrides how `orchivio` is started (words separated by
# spaces, no quoting: e.g. "python3 -m orchivio.cli").

set -eu

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

EMAIL=""; NAME=""; ORG=""; ROLE=""; STDIN_PW=0
while getopts "e:n:o:r:ph" opt; do
  case "$opt" in
    e) EMAIL=$OPTARG ;;
    n) NAME=$OPTARG ;;
    o) ORG=$OPTARG ;;
    r) ROLE=$OPTARG ;;
    p) STDIN_PW=1 ;;
    h) usage 0 ;;
    *) usage 2 ;;
  esac
done

if [ -n "${ORCHIVIO_BIN:-}" ]; then
  # shellcheck disable=SC2086 # a command with arguments, e.g. "python -m orchivio.cli"
  RUN=$ORCHIVIO_BIN
elif command -v orchivio >/dev/null 2>&1; then
  RUN=orchivio
elif command -v python3 >/dev/null 2>&1; then
  RUN="python3 -m orchivio.cli"
else
  RUN="python -m orchivio.cli"
fi

ask() { # ask VARNAME "Prompt" -- only on a terminal; elsewhere the missing value is reported below
  [ -t 0 ] || return 0
  printf '%s' "$2" >&2
  IFS= read -r "$1"
}

if [ -z "$EMAIL" ]; then ask EMAIL "E-mail: "; fi
if [ -z "$NAME" ]; then ask NAME "Display name: "; fi
if [ -z "$ORG" ] && [ -z "$ROLE" ] && [ -t 0 ] && [ "$STDIN_PW" = 0 ]; then
  ask ORG "Organization slug (empty = none): "
  if [ -n "$ORG" ]; then ask ROLE "Role [viewer]: "; ROLE=${ROLE:-viewer}; fi
fi
[ -n "$EMAIL" ] && [ -n "$NAME" ] || { echo "error: an e-mail address and a name are required" >&2; exit 2; }
if { [ -n "$ORG" ] && [ -z "$ROLE" ]; } || { [ -z "$ORG" ] && [ -n "$ROLE" ]; }; then
  echo "error: give both an organization and a role, or neither" >&2; exit 2
fi

ORG_ARGS=""
[ -n "$ORG" ] && ORG_ARGS="--org $ORG --role $ROLE"

if [ "$STDIN_PW" = 1 ]; then
  # shellcheck disable=SC2086
  exec $RUN user add --email "$EMAIL" --name "$NAME" $ORG_ARGS --password-stdin
fi

if [ -n "${ORCHIVIO_NEW_PASSWORD:-}" ]; then
  # shellcheck disable=SC2086
  printf '%s\n' "$ORCHIVIO_NEW_PASSWORD" | $RUN user add --email "$EMAIL" --name "$NAME" $ORG_ARGS --password-stdin
  exit $?
fi

[ -t 0 ] || { echo "error: no terminal to ask for a password on; use -p and pipe it in, or set ORCHIVIO_NEW_PASSWORD" >&2; exit 2; }
trap 'stty echo 2>/dev/null' EXIT INT TERM
stty -echo
printf 'Password: ' >&2; IFS= read -r PW1; printf '\n' >&2
printf 'Repeat the password: ' >&2; IFS= read -r PW2; printf '\n' >&2
stty echo
[ "$PW1" = "$PW2" ] || { echo "error: the passwords differ" >&2; exit 1; }
# shellcheck disable=SC2086
printf '%s\n' "$PW1" | $RUN user add --email "$EMAIL" --name "$NAME" $ORG_ARGS --password-stdin
