#!/bin/bash
SCRIPT="/roms/ports/meshcommandermax/MeshCommanderMax.py"
LOG="/roms/ports/meshcommandermax/last_run.log"
python3 -c "import pygame, RNS" 2>/dev/null || python3 "$SCRIPT" --install --auto 2>&1 | tee "$LOG"
python3 "$SCRIPT" 2>&1 | tee "$LOG"
