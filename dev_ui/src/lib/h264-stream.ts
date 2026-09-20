/**
 * WebCodecs H.264 camera player.
 *
 * Speaks the camera NALU WebSocket protocol: the first message is a JSON
 * header with the avc1 codec string and base64 avcC description, and each
 * binary message is a sequence of 4-byte-length-prefixed H.264 access
 * units. Decoded frames are drawn onto a caller-provided canvas.
 */

export function isWebCodecsAvailable(): boolean {
	if (typeof globalThis === 'undefined') {
		return false;
	}

	return typeof (globalThis as Record<string, unknown>)['VideoDecoder'] === 'function';
}

export type H264StreamHandlers = {
	onFirstFrame?: (info: { width: number; height: number }) => void;
	onError?: (reason: string) => void;
};

type H264StreamHeader = {
	type: string;
	codec: string;
	description: string;
	width: number;
	height: number;
	fps: number;
};

function base64ToBytes(encoded: string): Uint8Array {
	const binary = atob(encoded);
	const bytes = new Uint8Array(binary.length);

	for (let index = 0; index < binary.length; index += 1) {
		bytes[index] = binary.charCodeAt(index);
	}

	return bytes;
}

/** 'key' when the access unit contains an IDR picture, 'delta' otherwise.
 *
 * The wire carries AVCC access units: each NAL is a big-endian u32
 * length followed by the NAL's bytes, so there are no start codes to scan.
 */
function accessUnitChunkType(data: Uint8Array): 'key' | 'delta' {
	const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
	let offset = 0;

	while (offset + 4 <= data.length) {
		const length = view.getUint32(offset, false);

		// A malformed or truncated unit has no readable NAL header, so the
		// rest of the access unit is header data at best: report it as
		// delta.
		if (length === 0 || offset + 4 + length > data.length) {
			break;
		}

		const type = (data[offset + 4] ?? 0) & 0x1f;

		// 1 = non-IDR slice, 5 = IDR slice. SPS/PPS/SEI are header data;
		// the unit's type belongs to the next slice NAL.
		if (type === 1 || type === 5) {
			return type === 5 ? 'key' : 'delta';
		}

		offset += 4 + length;
	}

	return 'delta';
}

// Access units that arrive while the header is still pending are buffered
// instead of dropped. Bound the buffer so a pathological header stall cannot
// grow it without limit.
const MAX_PENDING_ACCESS_UNITS = 1024;

export class H264StreamPlayer {
	private socket: WebSocket | null = null;
	private decoder: VideoDecoder | null = null;
	private configured = false;
	private waitingForKey = false;
	private started = false;
	private frameIndex = 0;
	private frameDurationUs = 40000;
	private firstFrameSent = false;
	private canvasSizeSet = false;
	private pendingUnits: Uint8Array[] = [];

	constructor(
		private readonly canvas: HTMLCanvasElement,
		private readonly handlers: H264StreamHandlers = {}
	) {}

	get isRunning(): boolean {
		return this.started && this.socket !== null;
	}

	start(url: string): void {
		if (this.started) {
			return;
		}

		this.started = true;
		this.socket = new WebSocket(url);
		this.socket.binaryType = 'arraybuffer';

		this.socket.onmessage = (event: MessageEvent) => {
			void this.handleMessage(event.data);
		};
		this.socket.onerror = () => {
			this.fail('h264 stream connection failed');
		};
		this.socket.onclose = () => {
			// The page teardown path clears handlers before closing, so any
		// close that reaches here is a stream failure: report it so the
	// caller can fall back to MJPEG.
			this.fail('h264 stream closed');
		};
	}

	stop(): void {
		this.shutdown();
		this.started = false;
	}

	private fail(reason: string): void {
		this.shutdown();
		this.started = false;

		if (this.handlers.onError !== undefined) {
			this.handlers.onError(reason);
		}
	}

	private shutdown(): void {
		const decoder = this.decoder;

		this.decoder = null;
		this.configured = false;
		this.waitingForKey = false;
		this.pendingUnits = [];

		if (decoder !== null) {
			try {
				decoder.flush();
				decoder.close();
			} catch {
				// The decoder is already gone; nothing else to clean up.
			}
		}

		const socket = this.socket;
		this.socket = null;

		if (socket !== null) {
			socket.onmessage = null;
			socket.onerror = null;
			socket.onclose = null;
			socket.onopen = null;

			if (
				socket.readyState === WebSocket.OPEN ||
				socket.readyState === WebSocket.CONNECTING
			) {
				socket.close();
			}
		}
	}

	private async handleMessage(data: unknown): Promise<void> {
		if (typeof data === 'string') {
			await this.handleHeader(data);
			return;
		}

		if (data instanceof ArrayBuffer) {
			this.handleBinary(new Uint8Array(data));
		}
	}

	private async handleHeader(raw: string): Promise<void> {
		let header: H264StreamHeader;

		try {
			header = JSON.parse(raw) as H264StreamHeader;
		} catch {
			this.fail('malformed h264 stream header');
			return;
		}

		if (
			header.type !== 'header' ||
			typeof header.codec !== 'string' ||
			typeof header.description !== 'string'
		) {
			this.fail('malformed h264 stream header');
			return;
		}

		const config: VideoDecoderConfig = {
			codec: header.codec,
			description: base64ToBytes(header.description)
		};

		try {
			const support = await VideoDecoder.isConfigSupported(config);

			if (this.started === false) {
				// A stop landed while isConfigSupported was pending; the page
				// has already moved on, so don't create a decoder nothing
				// will close or report this stream's errors against the
				// camera that replaced it.
				this.shutdown();
				return;
			}

			if (support.supported !== true) {
				this.fail(`codec ${header.codec} is not supported`);
				return;
			}

			const decoder = new VideoDecoder({
				output: (frame: VideoFrame) => this.drawFrame(frame),
				error: (error: DOMException) => this.fail(String(error))
			});

			decoder.configure(config);
			this.decoder = decoder;
			this.configured = true;
			// WebCodecs requires the first chunk after configure() to be
			// 'key', so hold deltas until the stream's next keyframe.
			this.waitingForKey = true;

			if (typeof header.fps === 'number' && header.fps > 0) {
				this.frameDurationUs = Math.max(1, Math.round(1_000_000 / header.fps));
			}

			// Flush units that arrived while the header was pending, in
			// arrival order, through the normal keyframe and backpressure
			// path.
			const pending = this.pendingUnits;
			this.pendingUnits = [];

			for (const unit of pending) {
				// A decode error mid-flush fails the stream; the page is
				// tearing down, so don't push the rest of the buffer.
				// Snapshot into a local: the guard above narrows
				// `started`, and the narrowing survives the call below.
				const started = this.started as boolean;

				if (started === false) {
					break;
				}

				this.pushAccessUnit(unit);
			}
		} catch (error) {
			this.fail(error instanceof Error ? error.message : 'h264 decoder failed to start');
		}
	}

	private handleBinary(payload: Uint8Array): void {
		const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
		let offset = 0;

		while (offset + 4 <= payload.length) {
			const length = view.getUint32(offset, false);
			const start = offset + 4;

			if (length === 0 || start + length > payload.length) {
				return;
			}

			this.pushAccessUnit(payload.subarray(start, start + length));
			offset = start + length;
		}
	}

	private pushAccessUnit(accessUnit: Uint8Array): void {
		const decoder = this.decoder;

		if (decoder === null || this.configured === false) {
			// The header is still pending: buffer instead of dropping, so the
			// IDR-anchored catch-up burst survives, keeping only the newest
			// units.
			this.pendingUnits.push(accessUnit);

			const overflow = this.pendingUnits.length - MAX_PENDING_ACCESS_UNITS;

			if (overflow > 0) {
				this.pendingUnits.splice(0, overflow);
			}

			return;
		}

		const type = accessUnitChunkType(accessUnit);

		if (this.waitingForKey) {
			// Drop mid-GOP deltas rather than feed the decoder a delta
			// first chunk, which throws DataError. Dropped chunks do not
			// advance the timestamp counter.
			if (type !== 'key') {
				return;
			}

			this.waitingForKey = false;
		}

		const chunk = new EncodedVideoChunk({
			type,
			timestamp: this.frameIndex * this.frameDurationUs,
			data: accessUnit
		});

		this.frameIndex += 1;

		try {
			// The client is falling behind; drop frames rather than
			// accumulating decode latency.
			if (decoder.decodeQueueSize > 2) {
				return;
			}

			decoder.decode(chunk);
		} catch (error) {
			this.fail(error instanceof Error ? error.message : 'h264 decode failed');
		}
	}

	private drawFrame(frame: VideoFrame): void {
		const firstFrame = this.firstFrameSent;

		try {
			const context = this.canvas.getContext('2d');

			if (context !== null) {
					if (this.canvasSizeSet === false) {
						this.canvas.width = frame.codedWidth;
						this.canvas.height = frame.codedHeight;
						this.canvasSizeSet = true;
					}

				context.drawImage(frame, 0, 0, this.canvas.width, this.canvas.height);
			}
		} finally {
			frame.close();
		}

		if (firstFrame === false) {
			this.firstFrameSent = true;

			if (this.handlers.onFirstFrame !== undefined) {
				this.handlers.onFirstFrame({
					width: this.canvas.width,
					height: this.canvas.height
				});
			}
		}
	}
}
