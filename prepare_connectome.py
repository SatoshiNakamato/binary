"""Prepare the released male CNS connectome for the Flybrain container.

The raw Janelia release is downloaded only when build/graph.npz is absent.
The three source Feather files are removed after graph construction so the
runtime image contains the derived sparse graph rather than another copy of
the ~GB-scale raw edge table.
"""
from pathlib import Path
from urllib.request import Request, urlopen
import os
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BUILD = ROOT / "build"
GRAPH = BUILD / "graph.npz"

SOURCES = {
    "connectome-weights.feather": "https://storage.googleapis.com/flyem-male-cns/v1.0/connectome-data/flat-connectome/connectome-weights-male-cns-v1.0-minconf-0.5.feather",
    "body-annotations.feather": "https://storage.googleapis.com/flyem-male-cns/v1.0/connectome-data/flat-connectome/body-annotations-male-cns-v1.0-minconf-0.5.feather",
    "body-neurotransmitters.feather": "https://storage.googleapis.com/flyem-male-cns/v1.0/connectome-data/flat-connectome/body-neurotransmitters-male-cns-v1.0.feather",
}


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"connectome: using existing {dest.name}", flush=True)
        return

    tmp = dest.with_suffix(dest.suffix + ".download")
    if tmp.exists():
        tmp.unlink()

    print(f"connectome: downloading {dest.name}", flush=True)
    req = Request(url, headers={"User-Agent": "flybrain-connectome-builder/1.0"})
    started = time.time()
    with urlopen(req, timeout=120) as src, tmp.open("wb") as out:
        total = int(src.headers.get("Content-Length") or 0)
        done = 0
        last = started
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last >= 5:
                if total:
                    pct = done * 100 / total
                    print(f"  {done / 1e6:.0f}/{total / 1e6:.0f} MB ({pct:.1f}%)", flush=True)
                else:
                    print(f"  {done / 1e6:.0f} MB", flush=True)
                last = now
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, dest)
    print(f"connectome: {dest.name} ready in {time.time() - started:.0f}s", flush=True)


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    if GRAPH.exists() and GRAPH.stat().st_size > 0:
        print(f"connectome: {GRAPH} already exists; nothing to build", flush=True)
        return 0

    for name, url in SOURCES.items():
        download(url, DATA / name)

    print("connectome: building signed sparse graph", flush=True)
    subprocess.run([sys.executable, str(ROOT / "build_graph.py")], check=True)

    if not GRAPH.exists() or GRAPH.stat().st_size == 0:
        raise RuntimeError("build_graph.py finished without creating build/graph.npz")

    # The graph is the runtime artifact. Do not leave the large raw edge table
    # in the final container layer.
    for name in SOURCES:
        path = DATA / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    print(f"connectome: runtime graph ready: {GRAPH.stat().st_size / 1e6:.1f} MB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
