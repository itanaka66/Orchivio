"""The Studio's PWA icons are generated (scripts/make_pwa_icons.py) and must not drift from the checked-in files."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location("make_pwa_icons", ROOT / "scripts" / "make_pwa_icons.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checked_in_icons_match_the_generator():
    mod = _script()
    for name, data in mod.generate().items():
        assert (mod.OUT / name).read_bytes() == data, f"{name} is stale: run python scripts/make_pwa_icons.py"


def test_icons_are_valid_pngs_of_the_declared_sizes():
    import struct

    mod = _script()
    sizes = {"icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512}
    for name, data in mod.generate().items():
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", data[16:24])
        assert (w, h) == (sizes[name], sizes[name])
