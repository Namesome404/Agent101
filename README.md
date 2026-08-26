# Jarvis Backend

Jarvis is a multi-device voice-agent workspace. The repository is organized by
deployable component rather than by programming language.

On Windows, double-click `EV.bat` to start the complete project. Keep its
window open and press Enter to stop every managed EV process. Double-clicking
it again while EV is already running also provides a one-step shutdown.
(`Muse.bat` remains as a thin compatibility shim.)

## Components

| Directory | Responsibility |
| --- | --- |
| `server/` | Device protocol, agent runtime, providers, and the EV control plane |
| `auralis-toefl/` | Standalone macOS TOEFL speaking and article-shadowing desktop app |
| `esp32_speaker/` | ESP32 speaker firmware |
| `ws2812_led/` | WS2812 LED strip firmware |
| `ruview/`, `ruview_fw/` | RuView device integration and firmware tooling |
| `scripts/` | Repository-level operational scripts |
| `docs/` | Cross-component architecture and deployment documentation |

Local-only (never committed): `muse.db` / device bindings, `config.yaml` API keys,
`EV/API.md` (API memo; also copied to `Documents/Muse_EV_API.md` on this machine).
After clone, copy `server/main/EV/.env.example` → `.env`, put keys in
`data/.config.yaml` or the ignored server `config.yaml`, then bind devices in the UI.

The EV control plane is under `server/main/EV/`. Start it with:

```bash
cd server/main/EV
bash run_muse.sh
```

Layout:

```text
server/main/
├── EV/           # control plane + device adapters (:8002)
├── server/       # voice core (:8000 / OTA :8003)
└── digital-human/# browser terminal
```

See `docs/architecture.md` for boundaries and dependency rules.

## Auralis TOEFL

`auralis-toefl/` is an independent Tauri desktop application and does not open or control EV windows. It reuses the voice-provider approach for English conversation, updated TOEFL speaking practice, and sentence-by-sentence article shadowing. See [`auralis-toefl/README.md`](auralis-toefl/README.md) for configuration and build instructions.
