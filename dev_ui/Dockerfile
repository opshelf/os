FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm config set fetch-retries 5 && \
    npm config set fetch-retry-mintimeout 60000 && \
    npm config set fetch-retry-maxtimeout 300000 && \
    npm install --prefer-offline --no-audit

COPY . .

EXPOSE 8080

CMD ["npm", "run", "develop", "--", "--host", "0.0.0.0", "--port", "8080"]
