# Development UI

This SvelteKit application provides a lightweight operator interface for the Perpetual OS stack. It runs alongside the API and NATS services inside Docker (port `8080`) and can also be executed locally for rapid iteration.

## Prerequisites

- Node.js 20+
- npm 10+

## Configuration

The UI needs to know where the API is running. Set `VITE_API_BASE_URL` to the API base (defaults to `http://localhost:8000/api`).

```bash
cp .env.example .env
# edit .env if the API is not running on localhost:8000
```

## Local Development

```bash
npm install
npm run dev
```

The dev server listens on port `5173` by default. Update the `.env` file if the API lives elsewhere.

## Docker Development Stack

Use the top-level compose file to bring up the entire stack (NATS, API, and this UI on port `8080`).

```bash
# from the repository root
docker compose up --build dev_ui
```

Hot reloading is enabled through a bind mount. Dependencies install automatically when the container starts.

## Available npm Scripts

- `npm run dev` – start Vite in dev mode.
- `npm run check` – run Svelte type checking.
- `npm run build` – generate the production build.
- `npm run preview` – preview the production build locally.
