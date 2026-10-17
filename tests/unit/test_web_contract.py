"""The web Studio's generated inputs must match the backend (run scripts/export_web_contract.py)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location("export_web_contract", ROOT / "scripts" / "export_web_contract.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_web_files_are_up_to_date():
    mod = _script()
    for name, expected in mod.generate().items():
        actual = (mod.OUT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert actual == expected, f"{name} is stale: run python scripts/export_web_contract.py"


def test_contract_matches_validator_rules():
    import json

    from orchivio.model.flows import CLIENT_STEPS, SERVER_STEPS, handles_for

    c = json.loads((_script().OUT / "contract.json").read_text(encoding="utf-8"))
    assert set(c["serverSteps"]) == SERVER_STEPS and set(c["clientSteps"]) == CLIENT_STEPS
    assert all(set(c["handles"][t]) == handles_for(t) for t in c["stepTypes"])
    assert c["limits"]["maxIterations"] == 1000 and c["limits"]["maxSteps"] == 200
