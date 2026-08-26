# MeshCommanderMax v4

A standalone, framebuffer-based Reticulum mesh communicator for handheld Linux devices (R36S / GO-Super Gamepad compatible) paired with a Heltec V3 RNode LoRa interface. Operates directly via `/dev/fb0` without requiring an X server.

---

## Features

- **Direct Framebuffer UI:** Renders offscreen via Pygame and blits directly to `/dev/fb0` with runtime screen geometry and depth detection (supports 16-bit RGB565 and 32-bit RGBX panels).
- **Raw `evdev` Gamepad Input:** Direct asynchronous event loop reading from `/dev/input/event*`, bypassing SDL joystick limitations in headless/offscreen environments.
- **Reticulum Mesh Transport:** Fully native LoRa mesh communication via Reticulum Network Stack (RNS), supporting peer discovery, identity exchange, and link-based messaging.
- **Auto-Provisioning & Config Sync:** Automatic discovery of RNode serial devices (`/dev/ttyUSB*`, `/dev/ttyACM*`) and live synchronization of radio parameters from `radio.json` to `~/.reticulum/config`.
- **EmulationStation Integration:** Includes launcher integration and automated first-run setup for retro-gaming handheld distributions (ArkOS, AmberELEC, etc.).
- **Friend & Identity Persistence:** Retains local node identities and maps peer hashes to customizable aliases stored in `~/.reticulum/friends.json`.

---

## File Structure

| File | Purpose |
| :--- | :--- |
| `MeshCommanderMax.py` | Core application runtime, framebuffer UI, input parser, Reticulum interface manager, and CLI installer. |
| `MeshCommanderMax.sh` | Shell launcher script for EmulationStation; performs auto-install checks on launch and captures logs. |
| `radio.json` | LoRa physical layer hardware parameters applied to RNode interfaces during startup and install. |
| `handle.txt` *(optional)* | Broadcast handle/nickname advertised to mesh peers during announce events. Defaults to `anon`. |
| `keymap.json` *(optional)* | Custom button mapping overrides for third-party gamepad layouts and input node paths. |

---

## Radio Configuration (`radio.json`)

The node uses `radio.json` as its single source of truth for the physical LoRa transceiver setup:

```json
{
  "frequency": 915000000,
  "bandwidth": 125000,
  "txpower": 7,
  "spreadingfactor": 8,
  "codingrate": 5
}
```

### Parameters
- **Frequency:** `915000000` (915 MHz US/Canada ISM Band)
- **Bandwidth:** `125000` (125 kHz)
- **TX Power:** `7` dBm
- **Spreading Factor:** `8` (SF8)
- **Coding Rate:** `5` (4/5)

---

## Gamepad Controls (R36S / GO-Super)

| Button | Keycode | Function |
| :--- | :--- | :--- |
| **START** | `705` | Cycle panels (`STATUS` → `MESSAGES` → `PEERS` → `COMPOSE`) |
| **SELECT + START** | `704` + `705` | Exit application back to EmulationStation (press within 2 seconds) |
| **A** | `304` | Confirm / Select peer / Add character in Compose |
| **B** | `305` | Delete character / Back |
| **X** | `308` | Announce presence on mesh (`STATUS`/`MESSAGES`) / Save as Friend (`PEERS`) |
| **Y** | `307` | Send message to selected recipient (`COMPOSE`) |
| **L1 / R1** | `310` / `311` | Previous / Next panel |
| **L2 / R2** | `312` / `313` | Page Up / Page Down message history (scroll by 5) |
| **D-Pad ◄ / ►** | `546` / `547` | Cycle transport interfaces (`STATUS`) / Navigate character grid (`COMPOSE`) |
| **D-Pad ▲ / ▼** | `544` / `545` | Scroll message list / Navigate peer list / Navigate character rows |

---

## Installation & Setup

### 1. Automated Setup via EmulationStation Port
1. Place `MeshCommanderMax.py`, `MeshCommanderMax.sh`, and `radio.json` into `/roms/ports/meshcommandermax/`.
2. Ensure the launcher exists at `/roms/ports/MeshCommanderMax.sh` and has executable permissions (`chmod +x`).
3. Plug in the Heltec V3 LoRa module via the OTG port.
4. Launch **MeshCommanderMax** from the Ports menu. Missing dependencies (`pygame`, `rns`) will be installed automatically on first run.

### 2. Manual Installation
Run the interactive CLI installer:
```bash
python3 MeshCommanderMax.py --install
```
For non-interactive / headless setup:
```bash
python3 MeshCommanderMax.py --install --auto
```

### 3. Controller Calibration & Hardware Bring-Up
To verify gamepad keycodes or map non-standard controllers:
```bash
python3 MeshCommanderMax.py --diag-input
```
Map differing codes to `keymap.json`:
```json
{
  "A": 304,
  "B": 305,
  "X": 308,
  "Y": 307,
  "_devices": ["/dev/input/event2"]
}
```

---

## Diagnostics & Troubleshooting

- **Interface State:** Run `rnstatus` in a secondary SSH session to monitor real-time link states and packet counters.
- **Log Inspection:** Review `/roms/ports/meshcommandermax/last_run.log` for stdout/stderr logs from headless launcher sessions.
- **Serial Permissions:** Ensure the current user has read/write access to dialout / serial nodes (`sudo usermod -a -G dialout $USER`).
