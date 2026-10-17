"""Write the generated inputs of the web Studio's flow editor.

    python scripts/export_web_contract.py

* ``apps/web/src/flow/contract.json``: step types, handles, limits, id patterns (from the validator)
* ``apps/web/src/flow/sample-flow.json`` and ``sample-model.json``: the example model's flow and
  the model itself, used by the editor's demo mode and its tests

``tests/unit/test_web_contract.py`` fails if these files are stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchivio.model import AppModel, assign_element_ids, migrate_to_latest  # noqa: E402
from orchivio.model.flows import flow_contract  # noqa: E402

OUT = ROOT / "apps" / "web" / "src" / "flow"


def generate() -> dict[str, str]:
    raw = yaml.safe_load((ROOT / "examples" / "inventory-app" / "model.yaml").read_text(encoding="utf-8"))
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(raw)))
    # Element ids are random per run; pin them so the generated file is reproducible.
    dumped = json.loads(model.model_dump_json(exclude_none=True))
    _pin_ids(dumped)
    flow = dumped["modules"][0]["flows"][0]
    dump = lambda o: json.dumps(o, indent=2, ensure_ascii=False, sort_keys=True) + "\n"  # noqa: E731
    return {
        "contract.json": dump(flow_contract()),
        "sample-flow.json": dump(flow),
        "sample-model.json": dump(dumped),
    }


def _pin_ids(node, counter=None) -> None:
    counter = counter if counter is not None else [0]
    if isinstance(node, dict):
        if "element_id" in node:
            counter[0] += 1
            node["element_id"] = f"00000000-0000-4000-8000-{counter[0]:012d}"
        for v in node.values():
            _pin_ids(v, counter)
    elif isinstance(node, list):
        for v in node:
            _pin_ids(v, counter)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in generate().items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print("wrote", OUT / name)
