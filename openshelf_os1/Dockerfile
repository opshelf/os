FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
EXPOSE 5173
CMD ["npx", "vite", "preview", "--host", "0.0.0.0", "--port", "5173"]