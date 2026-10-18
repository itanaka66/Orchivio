import io

import yaml

from orchivio.cli import main
from tests_support import EXAMPLES


def run(*argv):
    out = io.StringIO()
    code = main([str(a) for a in argv], out)
    return code, out.getvalue()


SAMPLE = EXAMPLES / "model.yaml"


def test_validate(tmp_path):
    code, out = run("validate", SAMPLE)
    assert code == 0 and "the model is valid" in out
    bad = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    bad["security"]["access"] = []
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(bad), encoding="utf-8")
    code, out = run("validate", p)
    assert code == 1 and "no-access-rule" in out


def test_export_to_a_directory_and_zip_then_import_back(tmp_path):
    d, z, back = tmp_path / "model", tmp_path / "model.zip", tmp_path / "back.yaml"
    assert run("export", SAMPLE, d, "--assign-ids")[0] == 0
    assert (d / "modules/core/entities/Product.yaml").is_file()
    assert run("validate", d)[0] == 0
    assert run("export", d, z)[0] == 0
    first = z.read_bytes()
    assert run("export", d, z)[0] == 0 and z.read_bytes() == first  # reproducible
    assert run("import", z, back)[0] == 0
    code, out = run("diff", d, back)
    assert code == 0 and "no differences" in out
    assert yaml.safe_load(back.read_text(encoding="utf-8"))["application"]["key"] == "inventory"


def test_stale_canonical_files_are_removed_on_re_export(tmp_path):
    d = tmp_path / "model"
    run("export", SAMPLE, d)
    (d / "README.md").write_text("keep me", encoding="utf-8")
    spare = d / "modules/core/pages/old-page.yaml"
    spare.write_text("x: 1\n", encoding="utf-8")
    assert run("export", SAMPLE, d)[0] == 0
    assert not spare.exists() and (d / "README.md").exists()  # stale model files go; unrelated files stay
    assert run("validate", d)[0] == 0


def test_diff_reports_risks_and_exit_code(tmp_path):
    changed = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
    del changed["security"]["access"][1]["row_constraint"]
    p = tmp_path / "wider.yaml"
    p.write_text(yaml.safe_dump(changed), encoding="utf-8")
    code, out = run("diff", SAMPLE, p)
    assert code == 1 and "permission-expansion" in out and "A3" in out


def test_lint_and_usage_errors(tmp_path, capsys):
    code, out = run("lint", SAMPLE)
    assert code == 0 and "a11y-column-without-label" in out
    assert main(["validate", str(tmp_path / "missing.yaml")]) == 2
    assert "no such file" in capsys.readouterr().err
    broken = tmp_path / "broken.yaml"
    broken.write_text("schema_version: '1.1'\napplication: {key: x}\n", encoding="utf-8")
    assert main(["validate", str(broken)]) == 2
    assert "invalid model" in capsys.readouterr().err
