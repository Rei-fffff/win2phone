"""Fetch dependency wheels directly from a PyPI mirror.

Workaround for environments where `pip install` index resolution fails
(proxy 502 / pip 26 reporting "from versions: none") while plain HTTPS
to the mirror works. Downloads into ./.wheels for offline installation:

    python fetch_wheels.py
    .venv/Scripts/python.exe -m pip install --no-index --find-links .wheels mss pywin32 imageio-ffmpeg
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple/"
OUT = Path(__file__).parent / ".wheels"

# Python 3.13 / Windows x64 targets.
RULES = {
    "mss": lambda n: n.endswith(".whl") and "py3-none-any" in n,
    "pywin32": lambda n: n.endswith(".whl") and "cp313" in n and "win_amd64" in n,
    "imageio-ffmpeg": lambda n: n.endswith(".whl")
    and ("win_amd64" in n or "py3-none-any" in n),
}


def fetch(package: str, accept) -> None:
    url = urljoin(INDEX, package + "/")
    html = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace")
    links = [urljoin(url, h) for h in re.findall(r'href="([^"]+)"', html)]
    candidates = [u for u in links if accept(u.split("#")[0].split("/")[-1])]
    if not candidates:
        raise RuntimeError(f"no matching wheel found for {package}")
    picked = candidates[-1]          # mirror lists versions in ascending order
    name = picked.split("#")[0].split("/")[-1]
    data = urllib.request.urlopen(picked, timeout=180).read()
    (OUT / name).write_bytes(data)
    print(f"[+] {package}: {name}  ({len(data):,} bytes)")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for package, accept in RULES.items():
        try:
            fetch(package, accept)
        except Exception as exc:
            print(f"[!] {package}: {exc}")


if __name__ == "__main__":
    main()
