#!/bin/sh
# Starts the virtual display, the screen-sharing bridge and the agent, and restarts the agent if it stops.
set -u
PROFILE="$HOME/.signalpipe/chrome-profile"

# A restarted container keeps /tmp, and the old display's lock would stop the new one from starting.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
sleep 1
# The VNC server only listens inside this container; the dashboard reaches it through websockify,
# and the web server in front only lets signed-in users through.
x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -nopw -quiet &
websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 >/dev/null 2>&1 &

# Pass a container stop on to the agent so it closes the browser cleanly.
child=
trap 'echo "stopping"; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT

delay=30
while true; do
  # A lock left by a container that stopped abruptly would stop Chromium from opening the profile.
  rm -f "$PROFILE"/Singleton* 2>/dev/null
  node linkedin-agent.mjs run --server &
  child=$!
  wait "$child"
  status=$?
  child=
  echo "agent exited with status $status; restarting in ${delay}s"
  sleep "$delay"
  [ "$delay" -lt 600 ] && delay=$((delay * 2))
done
