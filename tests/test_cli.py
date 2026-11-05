import subprocess
import sys

import pytest

from fastbuild.cli import main


def test_cli_build(demo_config, tmp_path, monkeypatch, capsys):
    output = tmp_path / "cad"
    monkeypatch.setattr(
        sys,
        "argv",
        ["fastbuild", "build", str(demo_config), "--output", str(output)],
    )
    assert main() == 0
    captured = capsys.readouterr()
    assert "geometry_passed: 3 parts" in captured.out
    assert "Not checked:" in captured.out
    assert (output / "assembly.step").is_file()


def test_cli_failure_has_nonzero_exit(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fastbuild.cli",
            "build",
            str(tmp_path / "missing.yaml"),
            "--output",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "fastbuild: error:" in result.stderr
    assert not (tmp_path / "out").exists()


def test_enclosure_requires_explicit_plan_review(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fastbuild",
            "enclose",
            "brief.md",
            "--components",
            "components.yaml",
            "--plan",
            "intent.json",
            "--output",
            "result",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "--accept-plan" in capsys.readouterr().err
