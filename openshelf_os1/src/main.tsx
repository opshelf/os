import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import * as Sentry from '@sentry/react'
import './index.css'
import App from './App.tsx'

const sentryDsn = import.meta.env.VITE_SENTRY_DSN

if (sentryDsn) {
	Sentry.init({
		dsn: sentryDsn,
		integrations: [
			Sentry.browserTracingIntegration(),
			Sentry.replayIntegration(),
		],
		tracesSampleRate: parseFloat(import.meta.env.VITE_SENTRY_TRACES_SAMPLE_RATE || '0.1'),
		replaysSessionSampleRate: 0.1,
		replaysOnErrorSampleRate: 1.0,
		environment: import.meta.env.VITE_DEPLOYMENT_ENVIRONMENT || 'development',
		beforeSend(event) {
			if (event.request?.headers) {
				delete event.request.headers['Authorization']
				delete event.request.headers['Cookie']
			}
			return event
		},
	})
	console.log('Sentry initialized for dev UI')
} else {
	console.warn('Sentry DSN not configured for dev UI')
}

createRoot(document.getElementById('root')!).render(
	<StrictMode>
		<App />
	</StrictMode>,
)
