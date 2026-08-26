# Runtime

Runtime entrypoints supervise Muse processes without mixing process management
with device or agent logic.

- `start_muse.sh`: the production entrypoint. It uses `server/.venv` for the
  control plane and every Python child, and supervises the voice terminal.
- `start_voice_terminal.sh`: compatibility/debug entrypoint only. It refuses to
  fall back to the system Python and uses the same `server/.venv`.
- `start_camera_terminal.sh`: compatibility wrapper → `start_voice_terminal.sh`.

The voice terminal has no local wake-word model. When microphones are enabled it
runs as a continuous session; disable microphones from the device page to mute it.

The repository-root scripts remain compatibility wrappers.
