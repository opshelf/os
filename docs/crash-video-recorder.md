# Motion crash video recorder

The API keeps a rolling, in-memory history for every registered camera and attaches one WebM file per camera to caught motion-fault Sentry events. It also runs for simulated motor faults triggered from the development UI motors page.

The recorder reuses the JPEG frames already produced by the camera managers. It samples those frames at 3 FPS and retains at most 15 seconds and 16 MiB per camera. No video encoder runs during normal operation. After a crash, OpenCV's bundled VP8 encoder processes the frozen buffers sequentially as 640-pixel-wide WebM files, which limits the CPU spike on the Raspberry Pi 5 without adding a large system codec package to the device image.

Camera streams have separate foreground and background consumer counts. The IO-box camera captures at 3 FPS while only the recorder is using it and returns to its normal 12 FPS for a live UI stream. RTSP camera managers retain their existing 5 FPS capture rate.

## Configuration

The compose defaults are:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `CRASH_RECORDER_ENABLED` | `true` | Enables continuous rolling capture outside mock mode. |
| `CRASH_RECORDER_LOOKBACK_SECONDS` | `15` | Maximum history retained before a crash. |
| `CRASH_RECORDER_FPS` | `3` | Frames retained and encoded per second (supported range: 1–3). |
| `CRASH_RECORDER_MAX_WIDTH` | `640` | Maximum encoded video width (supported range: 160–640). |
| `CRASH_RECORDER_MAX_CAMERA_BUFFER_MIB` | `16` | In-memory JPEG budget per camera. |
| `CRASH_RECORDER_MAX_ATTACHMENT_MIB` | `4` | Maximum size of one camera attachment. |
| `CRASH_RECORDER_MAX_TOTAL_ATTACHMENT_MIB` | `14` | Maximum video attachment bytes for one event. |

Encoding and Sentry delivery run in a background task. Crash state and API responses do not wait for video processing. A `crash_recorder` context on the Sentry event records frame counts, buffered duration, attachment sizes, and per-camera failures or budget exclusions.

Sentry remains disabled in development and mock modes according to the existing operating-mode policy. A simulated crash in development exercises capture and encoding but does not upload the resulting temporary files.
