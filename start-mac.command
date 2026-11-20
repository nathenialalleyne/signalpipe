#!/bin/bash
# Double-click to start Signalpipe. The first start sets everything up, which takes a few minutes.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start-linux.sh "$@"
