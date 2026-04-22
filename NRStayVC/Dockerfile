FROM node:22.22.0-bookworm-slim

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-fund --no-audit

COPY index.js ./

ENV NODE_ENV=production

CMD ["node", "index.js"]
