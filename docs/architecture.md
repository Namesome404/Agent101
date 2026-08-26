# Jarvis architecture

## System model

Jarvis is split into five responsibility layers:

1. **Agent runtime** — dialogue, context, memory, intent, ASR, LLM, and TTS orchestration.
2. **Device adapters** — physical audio, video, sensors, playback, and device protocols.
3. **Agent tools** — weather, news, web reading, panels, vision queries, and device actions.
4. **Control plane** — configuration, provider selection, persistence, and management UI.
5. **Infrastructure** — WebSocket, MCP, media gateways, process supervision, and logging.

Hardware is not automatically a tool. A Camera or ESP32 is a device terminal.
Selected capabilities from that device may also be exposed to the agent as tools.

## Realtime interaction contract

The production voice path stays a cascaded streaming pipeline:

```text
VAD -> streaming ASR -> LLM capability call -> verified receipt -> streaming TTS
                                      |
                                      +-> desktop Scene protocol
                                      +-> IoT capability registry
```

- Voice turns expose a small capability-level toolset. Scene CRUD and device
  transports are implementation details, not separate prompt-level tools.
- Physical writes are serialized per device and successful only after an
  adapter returns explicit `meta.ok`; adapters may add independent readback.
- Desktop pages have stable IDs and are acknowledged by the Electron shell.
  Common pages use typed sections; raw HTML/CSS/JS is reserved for interactive
  layouts that actually need code.
- First-audible p50/p95 and per-stage timings come from real turn diagnostics,
  not synthetic provider benchmarks.

## Runtime topology

```text
Camera / ESP32 / Android
          |
          v
    Device adapters
          |
          v
 ASR -> Agent -> TTS
          |
          +------> Tools / MCP
          |
          +------> Control plane
```

External media gateways such as go2rtc belong to infrastructure. They transport
media but do not contain agent behavior.

## Repository boundaries

- `server/main/server` remains the voice-core runtime.
- `server/main/EV` contains custom EV orchestration and adapters.
- Firmware and independent clients remain separate deployable projects at the
  repository root.
- Local databases, credentials, generated audio, frames, and logs are runtime
  data and must not be committed.

## Dependency rules

- Device adapters may depend on `speech`, `control_plane`, and `common`.
- Tools may depend on public device capabilities but not device process internals.
- The control plane must not import a running device terminal.
- Speech providers must not depend on Camera- or ESP32-specific modules.
- Infrastructure must contain transport only, not dialogue or device policy.
- `app.py` is the composition root and may assemble all public packages.

## Adding a component

- New hardware adapter: `muse/devices/<device>/`
- New ASR, TTS, wake, or echo implementation: `muse/speech/<capability>/`
- New agent-callable capability: `muse/tools/`
- New API persistence or configuration feature: `muse/control_plane/`
- New transport or process integration: `muse/infrastructure/` or `muse/runtime/`
