#!/bin/sh
# Run a scrappadata script on the VPS — the 24/7 ingest host — with its settings.
#   ./vps_run.sh zingy.py intake loop          (the pm2 service "zingy-intake")
#   ./vps_run.sh zingy.py add --csv inbox/X    (what `zingy.py add` on the Mac sends here)
# Paths, exits and the box's address only; secrets stay in the API's .env and
# /etc/zingy (the cookie jar, and ZINGY_YT_PROXY=… for a paid proxy if one is set).
cd "$(dirname "$0")"
export ZINGY_HOST=vps
export ZINGY_REPO=/opt/zingy-ingest/catalog           # musicdata mirror + scripts/upload-catalog.sh
export ZINGY_API=http://127.0.0.1:5003/api
export ZINGY_INTAKE_LINE="${ZINGY_INTAKE_LINE:-replicate}"
export ZINGY_YT_EXITS=1081,1082                       # the box's tunnel, the Mac's (when awake)
export ZINGY_BOX_HOST=127.0.0.1 ZINGY_BOX_PORT=2222   # the box, through its own tunnel
export ZINGY_BOX_KEY=/root/.ssh/zingy_box
[ -f /etc/zingy/host.env ] && . /etc/zingy/host.env
# upload-catalog.sh `source`s its .env, and the API's has lines bash must not
# source: hand it only the Bunny + Genius keys, fresh from the API's on every run.
mkdir -p "$ZINGY_REPO/scripts" "$ZINGY_REPO/App/ZingyApp/Resources/musicdata/songs"
( umask 077; grep -E '^(BUNNY_[A-Z_]+|GENIUS_TOKEN)=' /var/www/zingy-api/.env > "$ZINGY_REPO/scripts/.env" )
exec ./venv/bin/python -u "$@"
