#!/usr/bin/env python3
"""
MeshCommanderMax v4 — R36S Mesh Communicator
Framebuffer UI (no X required). LoRa transport via Heltec V3 RNode.

Button map (GO-Super Gamepad / R36S, all keycodes verified):
  START   (705) — cycle panels
  SELECT  (704) — press then START within 2s = exit to ES
  A       (304) — confirm / add char / select peer
  B       (305) — delete / back
  X       (308) — announce to mesh (STATUS/MESSAGES) | save friend (PEERS)
  SELECT  (704) — press then START within 2s = exit to ES
  D-pad ◄► — cycle transport on STATUS panel
  Y       (307) — SEND message
  L1      (310) — previous panel
  R1      (311) — next panel
  L2      (312) — page up (messages)
  R2      (313) — page down (messages)
  UP      (544) — scroll / navigate
  DOWN    (545) — scroll / navigate
  LEFT    (546) — char cursor left
  RIGHT   (547) — char cursor right

Panels:  STATUS → MESSAGES → PEERS → COMPOSE

Issues fixed:
  #3  Friend list — save custom peer names, persisted to ~/.reticulum/friends.json
  #4  Announce button — X on STATUS broadcasts presence to mesh
  #6  Peers not showing — AspectFilter on announce handler, correct RNS API calls

Portability (for running on hardware other than the stock R36S):
  - Screen size + bit depth are read from /sys/class/graphics/fb0 at startup
    instead of assuming 640x480 (see detect_fb_geometry()).
  - Button codes and the evdev device path are overridable via a keymap.json
    file placed next to this script (see _load_keymap()).
  - Run with --diag-input to print raw evdev codes for hardware bring-up on
    new gamepad hardware — no pygame or RNS required for that mode.
  - Run with --install for a guided setup: checks/installs pygame + RNS,
    lets you set your broadcast handle (handle.txt), registers this script
    as an EmulationStation port, and finds/wires up the Heltec RNode's
    serial device path in ~/.reticulum/config -- using radio.json (if
    present next to this script) to fill in the actual LoRa frequency/
    bandwidth/spreadingfactor/codingrate automatically, no manual nano
    editing required. No pygame required for that mode either -- this and
    --diag-input both work before pygame/RNS are installed, which is the
    whole point of --install existing.
  - Diagnosing "opens fine but can't reach the mesh": RNS.Reticulum() does
    NOT raise an exception if a configured interface (like the RNode) fails
    to come up -- it just silently runs without it. Check with the `rnstatus`
    command (installed alongside RNS) while this is running, in another SSH
    session, to see each interface's real Up/Down state.
"""

from __future__ import annotations

import os, sys, time, threading, collections, glob, json, re
import struct as _struct
import select as _select

# pygame/RNS are only actually needed by main() (the normal messenger mode).
# --diag-input and --install must both work on a bare-metal fresh device
# BEFORE those are installed -- that's the whole point of --install -- so
# the imports are made non-fatal here and only hard-required inside main().
try:
    import pygame
except ImportError:
    pygame = None
try:
    import RNS
except ImportError:
    RNS = None

# ── Framebuffer env ────────────────────────────────────────────────────────
os.environ["SDL_VIDEODRIVER"] = "offscreen"
os.environ["SDL_NOMOUSE"]     = "1"
FB0_PATH = "/dev/fb0"

# ── Palette ────────────────────────────────────────────────────────────────
BG      = (10,  12,  18)
PANEL   = (18,  22,  32)
BORDER  = (40,  60,  90)
ACCENT  = (0,  200, 140)
ACCENT2 = (0,  160, 220)
SEL_BG  = (18,  45,  38)
WARN    = (220, 100,  40)
TEXT    = (210, 220, 230)
DIM     = (90,  100, 120)
GREEN   = (60,  220,  80)

# ── Layout ─────────────────────────────────────────────────────────────────
W, H      = 640, 480
TOPBAR    = 28
BOTBAR    = 24
PADX      = 10
CONTENT_Y = TOPBAR + 6

# ── Panels ─────────────────────────────────────────────────────────────────
PANEL_STATUS, PANEL_MESSAGES, PANEL_PEERS, PANEL_COMPOSE = 0, 1, 2, 3
PANEL_NAMES = ["STATUS", "MESSAGES", "PEERS", "COMPOSE"]
NUM_PANELS  = 4

# ── Compose charset ────────────────────────────────────────────────────────
CHARSET = list("abcdefghijklmnopqrstuvwxyz0123456789 .,!?-_/"
               "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
COLS    = 18

# ── Transport modes ────────────────────────────────────────────────────────
TRANSPORT_LORA = "LoRa (RNode)"
TRANSPORT_TCP  = "TCP (Internet)"
TRANSPORTS     = [TRANSPORT_LORA, TRANSPORT_TCP]

# ── Gamepad keycodes (raw evdev EV_KEY codes) ───────────────────────────────
# Defaults verified on the R36S GO-Super Gamepad. A different controller chip
# — even on identical silicon — can report different raw codes for the same
# physical buttons. To remap for new hardware:
#   1. Run:  python3 MeshCommanderMax.py --diag-input
#   2. Press each button and note the code(s) it prints.
#   3. Create keymap.json next to this script with only the keys that
#      differ, e.g.: {"A": 304, "B": 305, "_devices": ["/dev/input/event4"]}
def _load_keymap():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keymap.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"keymap.json present but failed to load ({e}); using built-in defaults")
        return {}

_KEYMAP = _load_keymap()

KEY_SELECT = _KEYMAP.get("SELECT", 704)
KEY_START  = _KEYMAP.get("START", 705)
KEY_A      = _KEYMAP.get("A", 304)
KEY_B      = _KEYMAP.get("B", 305)
KEY_X      = _KEYMAP.get("X", 308)
KEY_Y      = _KEYMAP.get("Y", 307)
KEY_L1     = _KEYMAP.get("L1", 310)
KEY_R1     = _KEYMAP.get("R1", 311)
KEY_L2     = _KEYMAP.get("L2", 312)
KEY_R2     = _KEYMAP.get("R2", 313)
KEY_UP     = _KEYMAP.get("UP", 544)
KEY_DOWN   = _KEYMAP.get("DOWN", 545)
KEY_LEFT   = _KEYMAP.get("LEFT", 546)
KEY_RIGHT  = _KEYMAP.get("RIGHT", 547)

# evdev nodes to try, in order — the first one that opens successfully wins.
# Override via keymap.json's "_devices" list if the gamepad shows up
# somewhere else on your hardware.
INPUT_DEVICES = _KEYMAP.get(
    "_devices", ["/dev/input/event2", "/dev/input/event3", "/dev/input/event1"]
)

# ── Broadcast handle ─────────────────────────────────────────────────────────
# What this device announces itself as to other peers (shows up in their
# PEERS panel before they've assigned it a friend name of their own). Set
# with `--install`, or just write a single line to handle.txt next to this
# script. Falls back to "anon" if neither exists yet.
def _load_handle():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "handle.txt")
    if os.path.exists(path):
        try:
            with open(path) as f:
                h = f.read().strip()
            if h:
                return h
        except Exception:
            pass
    return "anon"


HANDLE = _load_handle()


# ── Known-good radio parameters (optional) ──────────────────────────────────
# If radio.json exists next to this script, --install uses it to write a
# complete, correct [[RNode Interface]] block automatically -- no pasting,
# no manual nano editing, no template with placeholders to fill in by hand.
# Only meant to be created once real working values exist (e.g. copied from
# an already-working device's ~/.reticulum/config) -- see run_installer().
def _load_radio_defaults():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radio.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
        required = ("frequency", "bandwidth", "spreadingfactor", "codingrate")
        if all(k in d for k in required):
            return d
        print(f"radio.json is missing required keys {required}; ignoring it")
    except Exception as e:
        print(f"radio.json present but failed to load ({e}); ignoring it")
    return None


# ═══════════════════════════════════════════════════════════════════════════
class DirectInput:
    """
    Reads button events directly from /dev/input/event2 (evdev).
    Bypasses SDL joystick which is unreliable under offscreen SDL.
    Keycodes verified on R36S GO-Super Gamepad.
    """
    EV_KEY   = 1
    KEY_DOWN = 1

    def __init__(self):
        self.queue = collections.deque(maxlen=32)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        # Try each device in order; if one opens successfully, stay on it.
        # If it drops (unplugged, sleep-cycled, etc.) or its evdev node
        # hits EOF, retry from the top of the list.
        devices = INPUT_DEVICES
        while True:
            for dev in devices:
                fd = None
                try:
                    fd = open(dev, "rb", 0)
                    while True:
                        r, _, _ = _select.select([fd], [], [], 1.0)
                        if r:
                            data = fd.read(24)
                            if not data:
                                break  # EOF — device dropped, stop reading it
                            if len(data) == 24:
                                _, _, etype, code, value = _struct.unpack("llHHi", data)
                                if etype == self.EV_KEY and value == self.KEY_DOWN:
                                    self.queue.append(code)
                except Exception:
                    pass
                finally:
                    if fd is not None:
                        try:
                            fd.close()
                        except Exception:
                            pass
                time.sleep(0.5)
            time.sleep(1.0)  # all devices failed/dropped, wait before retrying

    def get(self):
        events = []
        while self.queue:
            events.append(self.queue.popleft())
        return events


# ═══════════════════════════════════════════════════════════════════════════
class _AnnounceHandler:
    """
    Top-level class so the instance is not garbage-collected.
    RNS may hold only a weak reference internally; storing it on MeshNode
    (self._announce_handler) keeps it alive for the lifetime of the node.
    """
    aspect_filter = "meshcmd.msg"

    def __init__(self, cb):
        self.cb = cb

    def received_announce(self, destination_hash, announced_identity, app_data):
        self.cb(destination_hash, announced_identity, app_data)


# ═══════════════════════════════════════════════════════════════════════════
class MeshNode:
    """
    Reticulum wrapper. All RNS callbacks run in background threads;
    shared state is protected by self._lock.
    """
    APP_NAME = "meshcmd"
    ASPECT   = "msg"

    def __init__(self, transport: str):
        self.transport      = transport
        self.reticulum      = None
        self.identity       = None
        self.dest           = None
        self.peers          = {}   # hash_str → {name, dest_hash, identity, link, last_seen}
        self.friends        = {}   # hash_str → custom name (persisted)
        self.inbox          = collections.deque(maxlen=80)
        self.link_state     = "INIT"
        self.has_unread     = False
        self.boot_error     = None
        self._lock          = threading.Lock()
        self._friends_path  = os.path.expanduser("~/.reticulum/friends.json")
        self._load_friends()
        self._thread        = threading.Thread(target=self._start, daemon=True)
        self._thread.start()

    # ── Friends persistence ──────────────────────────────────────────────
    def _load_friends(self):
        try:
            import json
            with open(self._friends_path, 'r') as f:
                self.friends = json.load(f)
        except Exception:
            self.friends = {}

    def save_friend(self, hash_str: str, name: str):
        import json
        self.friends[hash_str] = name
        with self._lock:
            if hash_str in self.peers:
                self.peers[hash_str]["name"] = name
        try:
            with open(self._friends_path, 'w') as f:
                json.dump(self.friends, f)
        except Exception:
            pass

    def is_friend(self, hash_str: str) -> bool:
        return hash_str in self.friends

    # ── Boot ────────────────────────────────────────────────────────────
    def _start(self):
        try:
            # Temporarily suppress signal() — RNS tries to register handlers
            # from this background thread which Python disallows.
            import signal as _sig
            _orig_signal   = _sig.signal
            _sig.signal    = lambda *a, **kw: None
            self.reticulum = RNS.Reticulum()
            _sig.signal    = _orig_signal  # restore immediately

            # Persist identity so our hash stays the same across restarts
            id_path = os.path.expanduser("~/.reticulum/meshcmd_identity")
            if os.path.exists(id_path):
                self.identity = RNS.Identity.from_file(id_path)
            else:
                self.identity = RNS.Identity()
                self.identity.to_file(id_path)

            self.dest = RNS.Destination(
                self.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                self.APP_NAME,
                self.ASPECT,
            )
            self.dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
            self.dest.set_link_established_callback(self._link_established)

            self._announce_handler = _AnnounceHandler(self._on_announce)
            RNS.Transport.register_announce_handler(self._announce_handler)
            with self._lock:
                self.link_state = "LISTENING"
            self.dest.announce(app_data=HANDLE.encode("utf-8")[:32])
        except Exception as exc:
            err = str(exc)
            if "signal" not in err.lower():
                print(f"RNS startup failed: {err}")
                with self._lock:
                    self.link_state = "ERROR"
                    self.boot_error = err

    # ── Incoming link ────────────────────────────────────────────────────
    def _link_established(self, link: RNS.Link):
        link.set_packet_callback(self._on_packet)
        link.set_resource_callback(lambda r: r.accept())
        link.set_link_closed_callback(self._link_closed)
        link.identify(self.identity)   # tell the remote who we are
        with self._lock:
            self.link_state = "LINKED"

    def _link_closed(self, link: RNS.Link):
        with self._lock:
            for peer in self.peers.values():
                if peer.get("link") is link:
                    peer["link"] = None
                    break
            still_active = any(
                p.get("link") and p["link"].status == RNS.Link.ACTIVE
                for p in self.peers.values()
            )
            if not still_active:
                self.link_state = "LISTENING"

    # ── Incoming packet ──────────────────────────────────────────────────
    def _on_packet(self, message: bytes, packet: RNS.Packet):
        text = message.decode("utf-8", errors="replace")
        try:
            remote_id    = packet.link.remote_identity
            sender_hash  = RNS.prettyhexrep(remote_id.hash) if remote_id else None
            sender_label = self._peer_name(sender_hash) if sender_hash else "unknown"
        except Exception:
            sender_hash  = None
            sender_label = "unknown"
        with self._lock:
            self.inbox.append((time.strftime("%H:%M"), sender_label, text, sender_hash))
            self.has_unread = True

    # ── Peer announced ───────────────────────────────────────────────────
    def _on_announce(self, dest_hash: bytes, identity, app_data):
        h    = RNS.prettyhexrep(dest_hash)
        name = app_data.decode("utf-8", errors="replace") if app_data else h[:8]
        # Use saved friend name if available
        name = self.friends.get(h, name)
        with self._lock:
            existing = self.peers.get(h, {})
            self.peers[h] = {
                "name":      name,
                "dest_hash": dest_hash,
                "identity":  identity,
                "link":      existing.get("link"),
                "last_seen": time.strftime("%H:%M"),
            }
            self.inbox.append((time.strftime("%H:%M"), "NET",
                               f"Peer: {name}", h))
            self.has_unread = True

    # ── Announce ─────────────────────────────────────────────────────────
    def announce(self):
        """Broadcast presence to mesh."""
        def _do():
            with self._lock:
                dest = self.dest
            if dest is None:
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "ERR",
                                       "RNS not ready", None))
                    self.has_unread = True
                return
            try:
                dest.announce(app_data=HANDLE.encode("utf-8")[:32])
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "NET",
                                       "Announced to mesh", None))
                    self.has_unread = True
            except Exception as exc:
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "ERR",
                                       f"Announce failed: {exc}", None))
                    self.has_unread = True
        threading.Thread(target=_do, daemon=True).start()

    # ── Outbound send ────────────────────────────────────────────────────
    def send_to(self, peer_hash: str, text: str):
        with self._lock:
            peer = self.peers.get(peer_hash)
        if not peer:
            return
        link = peer.get("link")
        if link and link.status == RNS.Link.ACTIVE:
            self._send_on_link(link, peer_hash, text)
        else:
            try:
                peer_dest = RNS.Destination(
                    peer["identity"],
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    self.APP_NAME,
                    self.ASPECT,
                )
                new_link = RNS.Link(
                    peer_dest,
                    established_callback=lambda l: self._outbound_ready(l, peer_hash, text),
                )
                with self._lock:
                    self.peers[peer_hash]["link"] = new_link
            except Exception as exc:
                with self._lock:
                    self.inbox.append((time.strftime("%H:%M"), "ERR",
                                       f"Link failed: {exc}", None))

    def _outbound_ready(self, link: RNS.Link, peer_hash: str, text: str):
        link.set_packet_callback(self._on_packet)
        link.set_resource_callback(lambda r: r.accept())
        link.set_link_closed_callback(self._link_closed)
        link.identify(self.identity)   # tell the remote who we are
        with self._lock:
            self.link_state = "LINKED"
            if peer_hash in self.peers:
                self.peers[peer_hash]["link"] = link
        self._send_on_link(link, peer_hash, text)

    def _send_on_link(self, link: RNS.Link, peer_hash: str, text: str):
        try:
            RNS.Packet(link, text.encode("utf-8")).send()
            peer_name = self._peer_name(peer_hash)
            with self._lock:
                self.inbox.append((time.strftime("%H:%M"), "ME",
                                   f"→{peer_name}: {text}", None))
        except Exception as exc:
            with self._lock:
                self.inbox.append((time.strftime("%H:%M"), "ERR",
                                   f"Send failed: {exc}", None))

    # ── Helpers ──────────────────────────────────────────────────────────
    def _peer_name(self, hash_str: str) -> str:
        with self._lock:
            p = self.peers.get(hash_str)
        return p["name"] if p else hash_str[:8]

    def clear_unread(self):
        with self._lock:
            self.has_unread = False

    def peer_list(self):
        with self._lock:
            return sorted(self.peers.items(),
                          key=lambda kv: kv[1].get("last_seen", ""), reverse=True)

    def iface_summary(self):
        if not self.reticulum:
            return []
        out = []
        for iface in RNS.Transport.interfaces:
            name = getattr(iface, "name", str(iface))
            if name.lower() == "reticulum":
                continue
            ok = getattr(iface, "online", False)
            out.append((name, "UP" if ok else "DOWN"))
        return out

    @property
    def short_hash(self) -> str:
        try:
            return RNS.prettyhexrep(self.dest.hash)[:12]
        except Exception:
            return "------"


# ═══════════════════════════════════════════════════════════════════════════
class Renderer:
    def __init__(self, screen, fonts):
        self.screen = screen
        self.F      = fonts

    def filled_rect(self, x, y, w, h, color, r=4):
        pygame.draw.rect(self.screen, color, (x, y, w, h), border_radius=r)

    def outlined_rect(self, x, y, w, h, fill, border, r=4, bw=1):
        self.filled_rect(x, y, w, h, fill, r)
        pygame.draw.rect(self.screen, border, (x, y, w, h), bw, border_radius=r)

    def txt(self, s, x, y, color=TEXT, font="md", anchor="topleft") -> int:
        surf = self.F[font].render(str(s).replace('\x00', ''), True, color)
        rect = surf.get_rect(**{anchor: (x, y)})
        self.screen.blit(surf, rect)
        return rect.width

    def hline(self, y, color=BORDER):
        pygame.draw.line(self.screen, color, (PADX, y), (W - PADX, y))

    # ── Top bar ──────────────────────────────────────────────────────────
    def draw_topbar(self, node, active_panel):
        self.filled_rect(0, 0, W, TOPBAR, PANEL)
        pygame.draw.line(self.screen, BORDER, (0, TOPBAR - 1), (W, TOPBAR - 1))
        self.txt("MESH", PADX, 6, ACCENT, "md")
        self.txt("CMD",  PADX + 46, 6, TEXT, "md")

        tab_w   = 80
        start_x = (W - tab_w * NUM_PANELS) // 2
        for i, name in enumerate(PANEL_NAMES):
            tx = start_x + i * tab_w
            if i == active_panel:
                self.filled_rect(tx - 2, 3, tab_w, TOPBAR - 6, ACCENT, r=3)
                self.txt(name, tx + tab_w // 2, 7, BG, "sm", anchor="midtop")
            else:
                self.txt(name, tx + tab_w // 2, 7, DIM, "sm", anchor="midtop")
            if i == PANEL_MESSAGES and node.has_unread and i != active_panel:
                pygame.draw.circle(self.screen, WARN, (tx + tab_w - 8, 8), 4)

        self.txt(node.short_hash, W - PADX, 7, DIM, "sm", anchor="topright")

    # ── Bottom hint bar ───────────────────────────────────────────────────
    def draw_botbar(self, hints):
        y = H - BOTBAR
        pygame.draw.line(self.screen, BORDER, (0, y), (W, y))
        self.filled_rect(0, y + 1, W, BOTBAR, PANEL)
        x = PADX
        for btn, label in hints:
            x += self.txt(f"[{btn}]", x, y + 4, ACCENT, "sm") + 2
            x += self.txt(f"{label} ", x, y + 4, DIM, "sm") + 6

    # ── STATUS panel ─────────────────────────────────────────────────────
    def draw_status(self, node, transport_idx):
        y = CONTENT_Y

        state = node.link_state
        sc    = ACCENT if state in ("LISTENING", "LINKED") else (WARN if state == "ERROR" else DIM)
        self.outlined_rect(PADX, y, W - PADX * 2, 36, PANEL, sc, r=5)
        self.txt("LINK",  PADX + 10, y + 4,  DIM, "sm")
        self.txt(state,   PADX + 10, y + 18, sc,  "md")
        if node.boot_error:
            self.txt(node.boot_error[:55], PADX + 80, y + 18, WARN, "sm")
        y += 44

        ifaces_list = node.iface_summary()  # cache — used twice below
        ifaces = {name: status for name, status in ifaces_list}
        self.txt("TRANSPORT", PADX, y, DIM, "sm")
        y += 16
        for i, name in enumerate(TRANSPORTS):
            selected = (i == transport_idx)
            if "RNode" in name or "LoRa" in name:
                iface_key = next((k for k in ifaces if "Heltec" in k or "RNode" in k or "LoRa" in k), None)
            else:
                iface_key = next((k for k in ifaces if "TCP" in k or "Internet" in k), None)
            online = ifaces.get(iface_key) == "UP" if iface_key else False
            fill   = SEL_BG if selected else PANEL
            border = ACCENT if selected else BORDER
            self.outlined_rect(PADX, y, W - PADX * 2, 30, fill, border, r=4)
            dot_c  = ACCENT if online else WARN
            pygame.draw.circle(self.screen, dot_c, (PADX + 14, y + 15), 5)
            self.txt(name, PADX + 28, y + 8, ACCENT if selected else TEXT, "sm")
            status_txt = ("UP" if online else "DOWN") + (" ◄ selected" if selected else "")
            self.txt(status_txt, W - PADX - 8, y + 8,
                     ACCENT if online else WARN, "sm", anchor="topright")
            y += 34

        y += 4
        self.hline(y); y += 8
        self.txt(f"INTERFACES ({len(ifaces_list)})", PADX, y, DIM, "sm")
        y += 16
        if not ifaces_list:
            self.txt("Starting Reticulum…", PADX + 8, y, DIM, "sm")
        else:
            for iname, status in ifaces_list:
                ok = status == "UP"
                self.outlined_rect(PADX, y, W - PADX * 2, 26, PANEL, BORDER, r=3)
                pygame.draw.circle(self.screen, ACCENT if ok else WARN,
                                   (PADX + 12, y + 13), 4)
                self.txt(iname,  PADX + 24,    y + 6, TEXT,               "sm")
                self.txt(status, W - PADX - 8, y + 6, ACCENT if ok else WARN,
                         "sm", anchor="topright")
                y += 30

        peers = node.peer_list()
        if peers:
            y += 4
            self.hline(y); y += 8
            self.txt(f"{len(peers)} peer(s) known", PADX, y, DIM, "sm")

    # ── MESSAGES panel ───────────────────────────────────────────────────
    def draw_messages(self, node, scroll):
        msgs  = list(node.inbox)
        end   = max(len(msgs) - scroll, 0)
        start = max(end - 9, 0)
        y     = CONTENT_Y

        if not msgs:
            self.txt("No messages yet", W // 2, H // 2, DIM, "md", anchor="center")
            return

        for ts, sender, body, _h in msgs[start:end]:
            is_me  = sender == "ME"
            is_sys = sender in ("NET", "ERR")
            bg     = SEL_BG if is_me else PANEL
            bc     = ACCENT if is_me else (DIM if is_sys else BORDER)
            hc     = ACCENT if is_me else (DIM if is_sys else ACCENT2)

            words = body.split()
            lines, cur = [], ""
            for w in words:
                if len(cur) + len(w) + 1 > 54:
                    lines.append(cur); cur = w
                else:
                    cur = (cur + " " + w).strip()
            if cur:
                lines.append(cur)

            row_h = 16 + len(lines) * 17
            if y + row_h > H - BOTBAR - 4:
                break

            self.outlined_rect(PADX, y, W - PADX * 2, row_h, bg, bc, r=5)
            self.txt(f"{sender}  {ts}", PADX + 8, y + 4, hc, "sm")
            for i, ln in enumerate(lines):
                self.txt(ln, PADX + 8, y + 16 + i * 17, TEXT, "sm")
            y += row_h + 4

        if scroll > 0:
            self.txt(f"↑ {scroll} older", W - PADX, CONTENT_Y, DIM, "sm", anchor="topright")

    # ── PEERS panel ──────────────────────────────────────────────────────
    def draw_peers(self, node, cursor, selected_hash):
        y     = CONTENT_Y
        peers = node.peer_list()

        self.txt("SELECT PEER  [X] save as friend", PADX, y, DIM, "sm"); y += 20

        if not peers:
            self.txt("Listening for announces…", W // 2, H // 2 - 10,
                     DIM, "md", anchor="center")
            self.txt("Press [X] on STATUS to announce yourself",
                     W // 2, H // 2 + 14, DIM, "sm", anchor="center")
            return

        vis_start = max(0, cursor - 5)
        for idx, (h, info) in enumerate(peers[vis_start: vis_start + 8]):
            real_idx  = idx + vis_start
            is_cursor = real_idx == cursor
            is_sel    = h == selected_hash
            is_friend = node.is_friend(h)
            fill      = SEL_BG if is_cursor else PANEL
            border    = ACCENT if is_cursor else (ACCENT2 if is_sel else BORDER)

            self.outlined_rect(PADX, y, W - PADX * 2, 36, fill, border, r=5)

            if is_friend:
                self.txt("★", PADX + 5, y + 10, ACCENT, "sm")
            if is_sel:
                self.txt("✓", PADX + 18, y + 10, ACCENT, "sm")
            if is_cursor:
                self.txt("▶", PADX + 32, y + 10, ACCENT, "sm")

            nc = ACCENT if is_cursor else (ACCENT2 if is_sel else TEXT)
            self.txt(info["name"],  PADX + 48, y + 4,  nc,  "md")
            self.txt(h[:16],        PADX + 48, y + 20, DIM, "sm")
            self.txt(info.get("last_seen", ""), W - PADX - 8, y + 10,
                     DIM, "sm", anchor="topright")

            lnk = info.get("link")
            if lnk:
                lc = GREEN if lnk.status == RNS.Link.ACTIVE else WARN
                self.txt("●", W - PADX - 28, y + 10, lc, "sm", anchor="topright")
            y += 40

        if selected_hash:
            sname = node.peers.get(selected_hash, {}).get("name", "?")
            self.txt(f"Target: {sname}",
                     W // 2, H - BOTBAR - 18, ACCENT, "sm", anchor="midtop")

    # ── COMPOSE panel ────────────────────────────────────────────────────
    def draw_compose(self, draft, cursor_pos, peer_name, blink):
        y = CONTENT_Y

        tc = ACCENT if peer_name else WARN
        tt = f"TO: {peer_name}" if peer_name else "TO: none — go to PEERS first"
        self.txt(tt, PADX, y, tc, "sm"); y += 18

        self.outlined_rect(PADX, y, W - PADX * 2, 76, PANEL, ACCENT, r=6)
        display  = draft + ("_" if blink else " ")
        chars_ln = 38
        lines    = [display[i:i + chars_ln] for i in range(0, max(len(display), 1), chars_ln)]
        for i, ln in enumerate(lines[-4:]):
            self.txt(ln, PADX + 10, y + 8 + i * 17, TEXT, "mono")
        y += 84

        self.txt("PICK:  ◄► char   ▲▼ row   [A] add   [B] del   [Y] SEND",
                 PADX, y, DIM, "sm"); y += 18

        cur_row   = cursor_pos // COLS
        start_row = max(0, cur_row - 1)
        char_w    = (W - PADX * 2) // COLS

        for r in range(start_row, start_row + 4):
            for col in range(COLS):
                i = r * COLS + col
                if i >= len(CHARSET):
                    break
                ch = CHARSET[i]
                cx = PADX + col * char_w
                cy = y + (r - start_row) * 26
                if cy > H - BOTBAR - 28:
                    break
                if i == cursor_pos:
                    self.filled_rect(cx, cy, char_w - 1, 24, ACCENT, r=3)
                    self.txt(ch, cx + char_w // 2, cy + 4, BG, "mono", anchor="midtop")
                else:
                    self.txt(ch, cx + char_w // 2, cy + 4, TEXT, "mono", anchor="midtop")


# ═══════════════════════════════════════════════════════════════════════════
def load_fonts():
    pygame.font.init()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ttf = next(
        (p for p in [
            os.path.join(script_dir, "terminus.ttf"),
            os.path.join(script_dir, "TerminusTTF.ttf"),
            os.path.join(script_dir, "font.ttf"),
        ] if os.path.exists(p)),
        None,
    )
    def load(size):
        if ttf:
            try: return pygame.font.Font(ttf, size)
            except Exception: pass
        return pygame.font.SysFont("monospace", size)
    return {"sm": load(13), "md": load(15), "lg": load(20), "mono": load(14)}


# ═══════════════════════════════════════════════════════════════════════════
def detect_fb_geometry(fb_num=0, fallback=(640, 480, 32)):
    """
    Read the real screen resolution and bit depth from sysfs instead of
    assuming the R36S's 640x480. This can't be done through SDL/pygame:
    SDL_VIDEODRIVER=offscreen is a virtual driver with no link to the real
    display hardware, so pygame.display.Info() just echoes back whatever
    size we request rather than reporting the panel's actual size.
    """
    base = f"/sys/class/graphics/fb{fb_num}"
    try:
        with open(f"{base}/virtual_size") as f:
            w_str, h_str = f.read().strip().split(",")
            w, h = int(w_str), int(h_str)
        with open(f"{base}/bits_per_pixel") as f:
            bpp = int(f.read().strip())
        if w > 0 and h > 0 and bpp in (16, 24, 32):
            return w, h, bpp
        print(f"fb{fb_num} reported {w}x{h}@{bpp}bpp — looks wrong, using fallback {fallback}")
    except Exception as e:
        print(f"Could not read fb{fb_num} geometry ({e}); using fallback {fallback}")
    return fallback


def run_input_diagnostic():
    """
    Standalone hardware bring-up helper — needs no pygame or RNS.
    Run:  python3 MeshCommanderMax.py --diag-input
    Press gamepad buttons one at a time; each prints the device node and
    raw keycode so you can fill in keymap.json for a new device.
    Ctrl+C to stop.
    """
    paths = sorted(glob.glob("/dev/input/event*"))
    if not paths:
        print("No /dev/input/event* nodes found.")
        return

    fds = {}
    for p in paths:
        try:
            fds[open(p, "rb", 0)] = p
        except Exception as e:
            print(f"  (skip) {p}: {e}")

    if not fds:
        print("Could not open any input device — try running with sudo.")
        return

    print(f"Listening on {len(fds)} device(s):")
    for p in fds.values():
        print(f"  {p}")
    print("\nPress each gamepad button one at a time. Ctrl+C to stop.\n")

    try:
        while True:
            r, _, _ = _select.select(list(fds.keys()), [], [], 1.0)
            for fd in r:
                data = fd.read(24)
                if len(data) == 24:
                    _, _, etype, code, value = _struct.unpack("llHHi", data)
                    if etype == 1 and value == 1:  # EV_KEY, key down
                        print(f"  {fds[fd]:<22} code={code}")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        for fd in fds:
            try:
                fd.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
_RNODE_TYPE_RE = re.compile(r"type\s*=\s*RNodeInterface", re.IGNORECASE)
_SECTION_RE = re.compile(r"\[\[[^\]]+\]\]")


def _find_rnode_block(text):
    """Return (start, end) span of the [[...]] section containing
    'type = RNodeInterface', or None if there isn't one yet."""
    type_match = _RNODE_TYPE_RE.search(text)
    if not type_match:
        return None
    start = 0
    for sm in _SECTION_RE.finditer(text):
        if sm.start() > type_match.start():
            break
        start = sm.start()
    end = len(text)
    for sm in _SECTION_RE.finditer(text, start + 1):
        end = sm.start()
        break
    return start, end


def _apply_port(block_text, port):
    """Set/replace the 'port = ...' line inside an interface block,
    leaving every other line (frequency, bandwidth, etc.) untouched."""
    if re.search(r"^\s*port\s*=", block_text, re.MULTILINE):
        return re.sub(r"^(\s*port\s*=\s*).*$", r"\1" + port, block_text, flags=re.MULTILINE)
    return block_text.rstrip() + f"\n  port = {port}\n"


def _check_port_permission(port):
    """
    Return a human-readable warning if the current user can't read/write the
    given device node, or None if access looks fine. This exists because
    RNS.Reticulum() swallows exactly this failure silently -- a permission
    error opening the serial port produces no visible error anywhere, just
    an interface that never comes up. Checking it explicitly, up front,
    turns that into an actual, actionable message.
    """
    try:
        if os.access(port, os.R_OK | os.W_OK):
            return None
        import grp
        import pwd
        st = os.stat(port)
        try:
            group_name = grp.getgrgid(st.st_gid).gr_name
        except Exception:
            group_name = str(st.st_gid)
        try:
            user_name = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            user_name = str(os.getuid())
        return (
            f"{port} exists but user '{user_name}' can't read/write it "
            f"(owned by group '{group_name}'). Fix with:\n"
            f"    sudo usermod -a -G {group_name} {user_name}\n"
            f"  then log out and back in (or reboot) — group changes don't\n"
            f"  apply to a session that's already running."
        )
    except Exception as e:
        return f"Could not check permissions on {port}: {e}"


def _sync_rnode_config():
    """
    Reconcile ~/.reticulum/config against radio.json + the currently-detected
    serial port on every normal launch -- not just during --install.

    This matters because the self-bootstrapping launcher only runs --install
    when pygame/RNS aren't importable yet. On a device that's already had
    them installed in an earlier session, dropping a fresh radio.json onto
    the SD card would otherwise never actually get applied -- the launcher
    would just skip straight past --install to a normal launch. Treating
    radio.json as live desired-state, checked every run, closes that gap.

    No-ops harmlessly if radio.json doesn't exist, nothing's plugged in, or
    ~/.reticulum/config doesn't exist yet (in which case run --install once
    to let RNS create its default config first).
    """
    try:
        radio_defaults = _load_radio_defaults()
        if not radio_defaults:
            return

        candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        if not candidates:
            return

        port = candidates[0]
        perm_warning = _check_port_permission(port)
        if perm_warning:
            print(f"WARNING: {perm_warning}")
        desired_block = (
            "[[RNode LoRa Interface]]\n"
            "  type = RNodeInterface\n"
            "  interface_enabled = true\n"
            f"  port = {port}\n"
            f"  frequency = {radio_defaults['frequency']}\n"
            f"  bandwidth = {radio_defaults['bandwidth']}\n"
            f"  txpower = {radio_defaults.get('txpower', 7)}\n"
            f"  spreadingfactor = {radio_defaults['spreadingfactor']}\n"
            f"  codingrate = {radio_defaults['codingrate']}\n"
        )

        config_path = os.path.expanduser("~/.reticulum/config")
        if not os.path.exists(config_path):
            return

        with open(config_path) as f:
            existing = f.read()

        span = _find_rnode_block(existing)
        if span:
            if existing[span[0]:span[1]].strip() == desired_block.strip():
                return  # already correct
            new_config = existing[:span[0]] + desired_block + "\n" + existing[span[1]:]
        else:
            new_config = existing.rstrip() + "\n\n" + desired_block + "\n"

        backup_path = f"{config_path}.bak.{int(time.time())}"
        with open(backup_path, "w") as f:
            f.write(existing)
        with open(config_path, "w") as f:
            f.write(new_config)
        print(f"RNode config synced from radio.json (backup: {backup_path})")
    except Exception as e:
        print(f"RNode config sync skipped ({e})")


def run_installer():
    """
    Guided setup — no pygame required for this mode.
    Run:  python3 MeshCommanderMax.py --install
    Checks/installs pygame + RNS, registers this script as an
    EmulationStation port, and finds/wires up the Heltec RNode's serial
    device path in ~/.reticulum/config. Safe to re-run any time — it
    backs up the config before touching it and skips steps already done.

    Run with --auto (e.g. python3 MeshCommanderMax.py --install --auto) to
    skip every prompt and use sensible defaults instead -- this is what the
    generated launcher .sh calls automatically the first time it notices
    pygame/RNS aren't installed yet, so a brand new device can go from
    "file copied onto the SD card" to "running" with no SSH session at all,
    aside from anything that genuinely needs a human (picking a handle,
    disambiguating which serial device is the RNode if more than one is
    plugged in).
    """
    auto = "--auto" in sys.argv
    print("=== MeshCommanderMax installer ===\n")

    # ---- 1. Dependencies ---------------------------------------------------
    print("[1/4] Checking dependencies...")
    missing = []
    try:
        import pygame  # noqa: F401
    except ImportError:
        missing.append("pygame")
    try:
        import RNS  # noqa: F401
    except ImportError:
        missing.append("rns")

    if missing:
        print(f"  Missing: {', '.join(missing)}")
        if auto:
            ans = "y"
        else:
            ans = input("  Install now with pip? [Y/n] ").strip().lower()
        if ans in ("", "y", "yes"):
            import subprocess
            for pkg in missing:
                print(f"  Installing {pkg}...")
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg, "--break-system-packages"]
                )
                if result.returncode != 0:
                    print(f"  pip install {pkg} failed — try 'sudo apt install python3-{pkg}' instead.")
        else:
            print("  Skipping — install manually before running MeshCommanderMax normally.")
    else:
        print("  pygame + RNS already installed.")

    # ---- 2. Broadcast handle -------------------------------------------------
    print("\n[2/4] Broadcast handle...")
    script_dir_for_handle = os.path.dirname(os.path.abspath(__file__))
    handle_path = os.path.join(script_dir_for_handle, "handle.txt")
    current_handle = _load_handle()
    if auto:
        new_handle = ""
        print(f"  (auto mode) Keeping '{current_handle}' — change it later with a normal --install.")
    else:
        try:
            new_handle = input(f"  Name to broadcast to peers [{current_handle}]: ").strip()
        except EOFError:
            new_handle = ""
    if new_handle:
        with open(handle_path, "w") as f:
            f.write(new_handle)
        print(f"  Saved — you'll show up as '{new_handle}' to other peers.")
    else:
        print(f"  Keeping '{current_handle}'.")

    # ---- 3. Ports registration ----------------------------------------------
    print("\n[3/4] Registering as an EmulationStation port...")
    app_dir = "/roms/ports/meshcommandermax"
    dest = os.path.join(app_dir, "MeshCommanderMax.py")
    launcher = "/roms/ports/MeshCommanderMax.sh"
    ports_dir = "/roms/ports"
    try:
        import shutil

        # Clean up remnants of the earlier plain-"MeshCommander" install.
        # A dangling gamelist.xml entry pointing at a deleted .sh has been
        # observed to hide the ENTIRE Ports category in EmulationStation,
        # not just the one broken entry -- so this isn't just tidiness.
        for old in ("/roms/ports/MeshCommander.sh", "/roms/ports/meshcommander"):
            if os.path.exists(old) and os.path.abspath(old) not in (
                os.path.abspath(launcher), os.path.abspath(app_dir)
            ):
                try:
                    if os.path.isdir(old):
                        shutil.rmtree(old)
                    else:
                        os.remove(old)
                    print(f"  Removed stale install remnant: {old}")
                except Exception as e:
                    print(f"  Could not remove {old}: {e}")

        gamelist = os.path.join(ports_dir, "gamelist.xml")
        if os.path.exists(gamelist):
            try:
                os.remove(gamelist)
                print("  Cleared gamelist.xml — EmulationStation will rebuild it fresh")
            except Exception as e:
                print(f"  Could not remove gamelist.xml: {e}")

        os.makedirs(app_dir, exist_ok=True)
        script_path = os.path.abspath(__file__)
        script_dir = os.path.dirname(script_path)
        if os.path.abspath(dest) != script_path:
            shutil.copy2(script_path, dest)
            print(f"  Copied script to {dest}")
            # keymap.json / handle.txt live next to the script -- without this,
            # a working controller remap or handle would silently vanish the
            # moment the script gets copied into place here.
            for companion in ("keymap.json", "handle.txt", "radio.json"):
                src = os.path.join(script_dir, companion)
                if os.path.exists(src) and os.path.abspath(src) != os.path.join(app_dir, companion):
                    shutil.copy2(src, os.path.join(app_dir, companion))
                    print(f"  Carried over {companion}")
        else:
            print(f"  Already in place at {dest}")

        # Self-bootstrapping launcher: on the very first launch (or after a
        # factory reset / SD re-image where the .py survived but pip
        # packages didn't), automatically run the unattended installer
        # before starting normally. This is what lets a brand new user just
        # drop the two files onto the SD card with no SSH session at all.
        #
        # Output is teed to a log file as well as the terminal: launching
        # from EmulationStation has no terminal attached at all, so without
        # this there'd be no way to see what happened (e.g. RNode permission
        # warnings) after swapping the OTG port from WiFi to the Heltec and
        # losing the SSH session that could've shown it live.
        log_path = os.path.join(app_dir, "last_run.log")
        launcher_body = (
            "#!/bin/bash\n"
            f'SCRIPT="{dest}"\n'
            f'LOG="{log_path}"\n'
            'python3 -c "import pygame, RNS" 2>/dev/null || python3 "$SCRIPT" --install --auto 2>&1 | tee "$LOG"\n'
            'python3 "$SCRIPT" 2>&1 | tee "$LOG"\n'
        )
        with open(launcher, "w") as f:
            f.write(launcher_body)
        os.chmod(launcher, 0o755)
        print(f"  Launcher ready at {launcher} (self-installs on first run if needed)")
        print(f"  Output is also logged to {log_path} for launches with no terminal attached")
    except Exception as e:
        print(f"  Could not set up the ports folder ({e}) — skipping this step.")

    # ---- 4. RNode interface --------------------------------------------------
    print("\n[4/4] RNode LoRa interface setup...")
    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not candidates:
        print("  No /dev/ttyUSB*/ttyACM* found. Plug in the Heltec RNode")
        print("  (and unplug anything else sharing the OTG port) then re-run:")
        print("    python3 MeshCommanderMax.py --install")
        return

    if len(candidates) == 1:
        port = candidates[0]
        print(f"  Found serial device: {port}")
    elif auto:
        port = candidates[0]
        print(f"  Multiple serial devices found, auto mode picked: {port}")
        print(f"  (others: {', '.join(candidates[1:])} — re-run without --auto to choose)")
    else:
        print("  Multiple serial devices found:")
        for i, c in enumerate(candidates):
            print(f"    [{i}] {c}")
        choice = input(f"  Which one is the RNode? [0-{len(candidates) - 1}]: ").strip()
        try:
            port = candidates[int(choice)]
        except Exception:
            port = candidates[0]
            print(f"  Invalid choice, defaulting to {port}")

    config_path = os.path.expanduser("~/.reticulum/config")
    perm_warning = _check_port_permission(port)
    if perm_warning:
        print(f"  WARNING: {perm_warning}")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    existing = ""
    if os.path.exists(config_path):
        with open(config_path) as f:
            existing = f.read()
        backup_path = f"{config_path}.bak.{int(time.time())}"
        with open(backup_path, "w") as f:
            f.write(existing)
        print(f"  Backed up existing config to {backup_path}")

    span = _find_rnode_block(existing)
    if span:
        block = existing[span[0]:span[1]]
        new_block = _apply_port(block, port)
        new_config = existing[:span[0]] + new_block + existing[span[1]:]
        print(f"  Updated the existing RNode Interface to use {port}")
        print("  (radio settings like frequency/bandwidth/spreadingfactor were left untouched)")
    else:
        print("  No RNode Interface found in the config yet.")
        radio_defaults = _load_radio_defaults()
        pasted = ""
        if radio_defaults:
            print("  Found radio.json — using its known-good radio settings automatically.")
        elif not auto:
            print("  Paste the [[RNode ...]] block from your other, already-working device below")
            print("  (its radio settings must match exactly for the two to talk to each other),")
            print("  or just press Enter on an empty line to insert a template to fill in by hand.")
            print("  Finish with a line containing only: END")
            lines = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if line.strip() == "END":
                    break
                lines.append(line)
            pasted = "\n".join(lines).strip()
        if radio_defaults:
            new_block = (
                "[[RNode LoRa Interface]]\n"
                "  type = RNodeInterface\n"
                "  interface_enabled = true\n"
                f"  port = {port}\n"
                f"  frequency = {radio_defaults['frequency']}\n"
                f"  bandwidth = {radio_defaults['bandwidth']}\n"
                f"  txpower = {radio_defaults.get('txpower', 7)}\n"
                f"  spreadingfactor = {radio_defaults['spreadingfactor']}\n"
                f"  codingrate = {radio_defaults['codingrate']}\n"
            )
        elif pasted:
            new_block = _apply_port(pasted, port)
        else:
            if auto:
                print("  (auto mode) Inserting a template — edit the radio settings by hand")
                print("  to match your other device before this will actually talk to it.")
            new_block = (
                "[[RNode LoRa Interface]]\n"
                "  type = RNodeInterface\n"
                "  interface_enabled = true\n"
                f"  port = {port}\n"
                "  frequency = 914875000    # MUST match your other device exactly\n"
                "  bandwidth = 125000       # MUST match your other device exactly\n"
                "  txpower = 7\n"
                "  spreadingfactor = 8      # MUST match your other device exactly\n"
                "  codingrate = 5           # MUST match your other device exactly\n"
            )
            print("  Inserted a template — edit ~/.reticulum/config by hand to match the")
            print("  frequency/bandwidth/spreadingfactor/codingrate on your other device.")
        new_config = existing.rstrip() + "\n\n" + new_block + "\n"

    with open(config_path, "w") as f:
        f.write(new_config)

    print(f"\nDone. RNode Interface now points at {port}.")
    print("Restart MeshCommanderMax (or reboot) to pick it up.")


# ═══════════════════════════════════════════════════════════════════════════
def main():
    global W, H

    if pygame is None or RNS is None:
        missing = [n for n, mod in (("pygame", pygame), ("RNS", RNS)) if mod is None]
        print(f"Missing dependency: {', '.join(missing)}")
        print(f"Run:  python3 {os.path.basename(__file__)} --install")
        sys.exit(1)

    _sync_rnode_config()

    W, H, FB_BPP = detect_fb_geometry(fallback=(W, H, 32))
    print(f"Using {W}x{H} @ {FB_BPP}bpp (fb0-detected)")

    pygame.display.init()
    pygame.joystick.init()

    screen = pygame.display.set_mode((W, H), 0)
    pygame.display.set_caption("MeshCommanderMax")
    pygame.mouse.set_visible(False)

    # For a 16bpp panel, render into a correctly-masked surface before writing
    # to /dev/fb0 — the framebuffer expects packed RGB565, not the 32-bit
    # RGBX bytes the original R36S path below produces. If colors come out
    # swapped on real hardware, try masks=(0x001F, 0x07E0, 0xF800, 0) (BGR565).
    fb16_surface = None
    if FB_BPP == 16:
        fb16_surface = pygame.Surface((W, H), depth=16,
                                       masks=(0xF800, 0x07E0, 0x001F, 0))

    # Threaded fb0 writer — never blocks the event loop
    class FB0Writer:
        def __init__(self, path):
            import queue as _q
            self.fb = open(path, "wb")
            self.q  = _q.Queue(maxsize=1)
            threading.Thread(target=self._run, daemon=True).start()
        def write(self, data):
            try: self.q.put_nowait(data)
            except Exception: pass
        def _run(self):
            while True:
                try:
                    self.fb.seek(0)
                    self.fb.write(self.q.get())
                except Exception: pass

    try:
        fb0 = FB0Writer(FB0_PATH)
    except Exception as e:
        print(f"Cannot open {FB0_PATH}: {e}")
        sys.exit(1)

    fonts    = load_fonts()
    renderer = Renderer(screen, fonts)

    transport_idx = 0
    node = MeshNode(TRANSPORTS[transport_idx])

    # Input via DirectInput (evdev) — SDL joystick unused under offscreen mode
    dinput      = DirectInput()
    select_held = False

    # ── UI state ─────────────────────────────────────────────────────────
    active_panel       = PANEL_STATUS
    msg_scroll         = 0
    peer_cursor        = 0
    selected_peer_hash = None
    compose_draft      = ""
    compose_cursor     = 0
    blink              = True
    last_blink         = time.time()

    HINTS = {
        PANEL_STATUS:   [("L1/R1", "panels"), ("◄►", "transport"), ("SEL+START", "exit")],
        PANEL_MESSAGES: [("↕", "scroll"), ("X", "announce"), ("A", "compose")],
        PANEL_PEERS:    [("↕", "move"), ("A", "select"), ("X", "★ friend")],
        PANEL_COMPOSE:  [("◄►▲▼", "char"), ("A", "add"), ("B", "del"), ("Y", "SEND")],
    }

    clock   = pygame.time.Clock()
    running = True

    while running:
        now = time.time()
        if now - last_blink > 0.5:
            blink      = not blink
            last_blink = now

        # ── pygame events (keyboard fallback for desktop testing) ─────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                k = event.key
                if k == pygame.K_q:
                    running = False
                elif k == pygame.K_TAB:
                    active_panel = (active_panel + 1) % NUM_PANELS
                    if active_panel == PANEL_MESSAGES:
                        node.clear_unread()
                elif active_panel == PANEL_MESSAGES:
                    if k == pygame.K_UP:
                        msg_scroll = min(msg_scroll + 1, max(0, len(node.inbox) - 5))
                    elif k == pygame.K_DOWN:
                        msg_scroll = max(0, msg_scroll - 1)
                elif active_panel == PANEL_PEERS:
                    peers = node.peer_list()
                    if k == pygame.K_UP:
                        peer_cursor = max(0, peer_cursor - 1)
                    elif k == pygame.K_DOWN:
                        peer_cursor = min(max(len(peers) - 1, 0), peer_cursor + 1)
                    elif k == pygame.K_RETURN and peers:
                        selected_peer_hash = peers[peer_cursor][0]
                        active_panel       = PANEL_COMPOSE
                elif active_panel == PANEL_COMPOSE:
                    if k == pygame.K_LEFT:
                        compose_cursor = max(0, compose_cursor - 1)
                    elif k == pygame.K_RIGHT:
                        compose_cursor = min(len(CHARSET) - 1, compose_cursor + 1)
                    elif k == pygame.K_UP:
                        compose_cursor = max(0, compose_cursor - COLS)
                    elif k == pygame.K_DOWN:
                        compose_cursor = min(len(CHARSET) - 1, compose_cursor + COLS)
                    elif k == pygame.K_RETURN:
                        compose_draft += CHARSET[compose_cursor]
                    elif k == pygame.K_BACKSPACE:
                        compose_draft = compose_draft[:-1]
                    elif k == pygame.K_y and compose_draft.strip() and selected_peer_hash:
                        node.send_to(selected_peer_hash, compose_draft.strip())
                        compose_draft = ""
                        active_panel  = PANEL_MESSAGES
                        msg_scroll    = 0
                        node.clear_unread()

        # ── Direct evdev input ────────────────────────────────────────────
        for code in dinput.get():

            if code == KEY_SELECT:   # SELECT — arms exit combo only
                select_held = time.time()

            elif code == KEY_START:   # START
                if select_held and (time.time() - select_held) < 2.0:
                    # SELECT+START combo — exit to EmulationStation
                    pygame.quit()
                    os.system("sudo systemctl start emulationstation.service &")
                    sys.exit(0)
                else:
                    # Normal START — cycle panels
                    select_held = False
                    active_panel = (active_panel + 1) % NUM_PANELS
                    if active_panel == PANEL_MESSAGES:
                        node.clear_unread()

            # X — announce (STATUS/MESSAGES) | save friend (PEERS)
            elif code == KEY_X:
                select_held = False
                if active_panel in (PANEL_STATUS, PANEL_MESSAGES):
                    node.announce()
                elif active_panel == PANEL_PEERS:
                    peers = node.peer_list()
                    if peers:
                        h, info = peers[peer_cursor]
                        node.save_friend(h, info["name"])
                        with node._lock:  # Fix 2: use lock
                            node.inbox.append((time.strftime("%H:%M"), "NET",
                                               f"★ Saved {info['name']}", None))
                            node.has_unread = True

            # A — confirm / add char
            elif code == KEY_A:
                select_held = False
                if active_panel == PANEL_MESSAGES:
                    active_panel = PANEL_COMPOSE
                elif active_panel == PANEL_PEERS:
                    peers = node.peer_list()
                    if peers:
                        selected_peer_hash = peers[peer_cursor][0]
                        active_panel       = PANEL_COMPOSE
                elif active_panel == PANEL_COMPOSE:
                    compose_draft += CHARSET[compose_cursor]

            # B — back / delete
            elif code == KEY_B:
                select_held = False
                if active_panel == PANEL_COMPOSE:
                    if compose_draft:
                        compose_draft = compose_draft[:-1]
                    else:
                        active_panel = PANEL_MESSAGES

            # Y — send
            elif code == KEY_Y:
                select_held = False
                if active_panel == PANEL_COMPOSE and compose_draft.strip():
                    if selected_peer_hash:
                        node.send_to(selected_peer_hash, compose_draft.strip())
                    compose_draft = ""
                    active_panel  = PANEL_MESSAGES
                    msg_scroll    = 0
                    node.clear_unread()

            # L1 (310) — previous panel
            elif code == KEY_L1:
                select_held = False
                active_panel = (active_panel - 1) % NUM_PANELS
                if active_panel == PANEL_MESSAGES:
                    node.clear_unread()

            # R1 (311) — next panel
            elif code == KEY_R1:
                select_held = False
                active_panel = (active_panel + 1) % NUM_PANELS
                if active_panel == PANEL_MESSAGES:
                    node.clear_unread()

            # L2 (312) — page up in messages (jump 5)
            elif code == KEY_L2:
                select_held = False
                if active_panel == PANEL_MESSAGES:
                    msg_scroll = min(msg_scroll + 5, max(0, len(node.inbox) - 5))

            # R2 (313) — page down in messages (jump 5)
            elif code == KEY_R2:
                select_held = False
                if active_panel == PANEL_MESSAGES:
                    msg_scroll = max(0, msg_scroll - 5)

            # UP
            elif code == KEY_UP:
                select_held = False
                if active_panel == PANEL_MESSAGES:
                    msg_scroll = min(msg_scroll + 1, max(0, len(node.inbox) - 5))
                elif active_panel == PANEL_PEERS:
                    peer_cursor = max(0, peer_cursor - 1)
                elif active_panel == PANEL_COMPOSE:
                    compose_cursor = max(0, compose_cursor - COLS)

            # DOWN
            elif code == KEY_DOWN:
                select_held = False
                if active_panel == PANEL_MESSAGES:
                    msg_scroll = max(0, msg_scroll - 1)
                elif active_panel == PANEL_PEERS:
                    peers = node.peer_list()
                    peer_cursor = min(max(len(peers) - 1, 0), peer_cursor + 1)
                elif active_panel == PANEL_COMPOSE:
                    compose_cursor = min(len(CHARSET) - 1, compose_cursor + COLS)

            # LEFT
            elif code == KEY_LEFT:
                select_held = False
                if active_panel == PANEL_STATUS:
                    transport_idx = (transport_idx - 1) % len(TRANSPORTS)
                    node.transport = TRANSPORTS[transport_idx]
                elif active_panel == PANEL_COMPOSE:
                    compose_cursor = max(0, compose_cursor - 1)

            # RIGHT
            elif code == KEY_RIGHT:
                select_held = False
                if active_panel == PANEL_STATUS:
                    transport_idx = (transport_idx + 1) % len(TRANSPORTS)
                    node.transport = TRANSPORTS[transport_idx]
                elif active_panel == PANEL_COMPOSE:
                    compose_cursor = min(len(CHARSET) - 1, compose_cursor + 1)

        # ── Draw ──────────────────────────────────────────────────────────
        screen.fill(BG)
        renderer.draw_topbar(node, active_panel)

        if active_panel == PANEL_STATUS:
            renderer.draw_status(node, transport_idx)
        elif active_panel == PANEL_MESSAGES:
            renderer.draw_messages(node, msg_scroll)
        elif active_panel == PANEL_PEERS:
            renderer.draw_peers(node, peer_cursor, selected_peer_hash)
        elif active_panel == PANEL_COMPOSE:
            peer_name = None
            if selected_peer_hash:
                with node._lock:
                    peer_name = node.peers.get(selected_peer_hash, {}).get("name")
            renderer.draw_compose(compose_draft, compose_cursor, peer_name, blink)

        renderer.draw_botbar(HINTS[active_panel])
        if fb16_surface is not None:
            fb16_surface.blit(screen, (0, 0))
            fb0.write(fb16_surface.get_buffer().raw)
        else:
            fb0.write(pygame.image.tobytes(screen, "RGBX"))
        clock.tick(20)

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    if "--diag-input" in sys.argv:
        run_input_diagnostic()
    elif "--install" in sys.argv:
        run_installer()
    else:
        main()
