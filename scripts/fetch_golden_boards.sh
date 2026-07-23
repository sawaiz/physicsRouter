#!/usr/bin/env bash
# Fetch open hardware KiCad boards into third_party/golden/ (gitignored).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/third_party/golden"
mkdir -p "$DEST"
cd "$DEST"

clone() {
  local url="$1" name="$2"
  if [[ -d "$name/.git" ]]; then
    echo "exists $name"
    return 0
  fi
  echo "=== $name ==="
  git clone --depth 1 "$url" "$name"
}

clone https://github.com/antmicro/jetson-nano-baseboard.git antmicro-jetson-nano
clone https://gitlab.com/openipmc/openipmc-hw.git openipmc-hw
clone https://gitlab.com/librespacefoundation/satnogs-comms/satnogs-comms-hardware.git satnogs-comms
clone https://gitlab.com/librespacefoundation/pq9ish/pq9-devboard.git pq9-devboard
clone https://gitlab.com/openflexure/openflexure-simple-illumination.git openflexure-illum
clone https://gitlab.com/filipayazi/ofm-led.git ofm-led
clone https://github.com/muonTelescope/mppcInterface.git mppcInterface

# Pin complete 4-layer human golden (v1.3) into examples if missing
if [[ ! -f "$ROOT/examples/mppc-interface/mppcInterface_v1.3.kicad_pcb" ]] && [[ -d mppcInterface/.git ]]; then
  mkdir -p "$ROOT/examples/mppc-interface"
  git -C mppcInterface show 580c61d:pcb/mppcInterface.kicad_pcb \
    > "$ROOT/examples/mppc-interface/mppcInterface_v1.3.kicad_pcb" || true
  git -C mppcInterface show 580c61d:pcb/mppcInterface.kicad_pro \
    > "$ROOT/examples/mppc-interface/mppcInterface_v1.3.kicad_pro" 2>/dev/null || true
fi

if [[ ! -d kicad-demos/vme-wren ]]; then
  echo "=== kicad demos (sparse) ==="
  rm -rf kicad-src
  git clone --depth 1 --filter=blob:none --sparse https://gitlab.com/kicad/code/kicad.git kicad-src
  (cd kicad-src && git sparse-checkout set demos)
  rm -rf kicad-demos
  mkdir -p kicad-demos
  cp -R kicad-src/demos/* kicad-demos/
  rm -rf kicad-src
fi

echo "Boards:"
find "$DEST" -name '*.kicad_pcb' | wc -l
echo "Done. Run: python scripts/golden_corpus_analyze.py --route-easy"
