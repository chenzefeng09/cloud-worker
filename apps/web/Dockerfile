# syntax=docker/dockerfile:1.7
FROM node:22-alpine

WORKDIR /app

# Node Alpine images already ship a non-root ``node`` user (uid 1000); reuse it
# rather than creating a duplicate.
COPY --chown=node:node package.json package-lock.json* ./
RUN npm ci || npm install

COPY --chown=node:node . .

ENV NODE_ENV=development
USER node

EXPOSE 5173

CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]
