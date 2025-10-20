FROM node:20-alpine as base

WORKDIR /app

COPY package*.json ./
RUN npm ci && npm cache clean --force

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]