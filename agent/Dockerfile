# The LinkedIn agent, packaged to run next to Signalpipe on the same server.
# Chromium runs as a normal window on a virtual display; you can see and use that window from the
# dashboard (Settings, "Open the agent's screen") to sign in or to deal with a LinkedIn security check.
FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium xvfb x11vnc novnc websockify \
      fonts-liberation fonts-noto-color-emoji ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /agent
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY linkedin-agent.mjs start.sh ./
RUN chmod 755 start.sh && mkdir -p /home/node/.signalpipe && chown -R node:node /home/node/.signalpipe
USER node
# --no-sandbox: Chromium's own sandbox needs kernel features Docker does not grant by default.
ENV DISPLAY=:99 \
    LINKEDIN_SERVER=1 \
    LINKEDIN_MODE=attach \
    LINKEDIN_CHROME_PATH=/usr/bin/chromium \
    LINKEDIN_CHROME_ARGS="--no-sandbox --password-store=basic --hide-crash-restore-bubble"
EXPOSE 6080
CMD ["./start.sh"]
