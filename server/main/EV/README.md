# Muse

Muse is the custom control plane and device-orchestration layer built around the
standard Xiaozhi runtime.

## Directory layout

```text
muse/
├── app.py                 # FastAPI composition root
├── common/                # Shared paths and primitives
├── control_plane/         # Database and Xiaozhi configuration contracts
├── devices/
│   ├── camera/            # RTSP audio/video, terminal, vision, pose, MCP
│   ├── esp32/             # Muse-side ESP32 adapters
│   └── iot/               # Typed device capability registry and receipts
├── speech/
│   ├── asr/               # Streaming ASR adapters
│   ├── echo/              # Echo and barge-in algorithms
│   ├── tts/               # Streaming TTS bridge and worker
│   └── voice_core/        # Reusable dialog→segment→duplex→PCM sink pipeline
├── tools/                 # Agent-facing tools and panel enrichment
├── infrastructure/        # Core proxy and transport infrastructure
├── diagnostics/           # Offline diagnostic reports
├── runtime/               # Process startup implementations
├── ui/                    # Management frontend
├── data/                  # Local models and device configuration
├── tmp/                   # Runtime logs, audio, and generated frames
└── vendor/                # Bundled third-party assets
```

## Processes

| Process | Port | Module |
| --- | --- | --- |
| Muse control plane | `8002` | `app.py` |
| Voice terminal | none | `devices.voice.terminal` |
| Voice core | `8000` | sibling `server` project |
| LED control | device HTTP | `devices.coding.led` (write + status readback) |

Use `run_muse.sh` as the stable entrypoint. It delegates process management to
`runtime/start_muse.sh`.

## Voice action protocol

Voice turns expose exactly three model-facing functions:

- `conversation_reply`: answer or ask one clarification without side effects.
- `task_control`: bounded information retrieval and core long-running tasks.
- `object_control`: a constant `inspect / apply / invoke` protocol for UI,
  devices, canvases, apps, artifacts, and installed skills.

Capabilities are registered at runtime in `control_plane.object_registry` and
are returned only by `object_control.inspect`. Adding a device or skill must not
add a model-facing function, enum value, or routing-prompt row. Existing
`surface_control`, `device_control`, and `canvas_control` modules are internal
adapters and compatibility entrypoints. Every mutation receipt is bound to a
stable `target_id`, `target_name`, `target_kind`, and `target_owner` with
`verified_target=true`.

### Work Agent runtime

Engineering work is exposed as the stable runtime object `project.active`, not
as a provider-specific model tool. EV creates a versioned work order, waits for
conversation confirmation, then starts `devices.coding.agent_runtime`. The
runtime prefers Codex App Server over local stdio and can fall back to the
legacy Claude adapter when automatic provider selection is enabled and the
primary provider fails before changing files.

Provider events are normalized into planning, reading, editing, checking and
terminal states. A borderless 152×224 `work-hud` appears beside the existing
status window only while work is active; it never resizes the status window.
Completion is accepted only from the runtime result plus a SHA-256 before/after
filesystem receipt. PCB and CAD providers can register more runtime objects
later without changing the three model-facing function schemas.

## Runtime capabilities

- `GET /api/agents/{id}/voice/health`: real-turn p50/p95 latency, per-stage
  bottlenecks, failure/interruption counts, and supervised process liveness.
- `GET /api/iot/devices` and `POST /api/iot/devices/{id}/commands`: device
  capability discovery and typed command receipts for UI and integrations.
