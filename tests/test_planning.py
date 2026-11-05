import copy
import io
import json
from pathlib import Path
from urllib.error import URLError

import pytest

from fastbuild import planning
from fastbuild.config import DesignError, read_document

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def planner_response():
    return read_document(EXAMPLES / "enclosure-intent.json")


def mock_local_model(monkeypatch, response):
    requests = []

    class LocalModel:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(
                json.dumps({"message": {"content": json.dumps(response)}}).encode()
            )

    monkeypatch.setattr(planning, "build_opener", lambda *handlers: LocalModel())
    return requests


def test_natural_language_planner_returns_reviewable_data_not_code(
    monkeypatch, planner_response, tmp_path
):
    requests = mock_local_model(monkeypatch, planner_response)
    brief = EXAMPLES / "enclosure-brief.md"
    manifest = EXAMPLES / "components.yaml"
    output = tmp_path / "plan.json"
    result = planning.plan(brief, manifest, "local-test-model", output)
    assert result["layout_axis"] == "x"
    assert requests[0].full_url == "http://127.0.0.1:11434/api/chat"
    payload = json.loads(requests[0].data)
    assert "stream" in payload and not payload["stream"]
    assert "format" in payload
    # Inference receives the brief and component names, not STEP bytes or paths.
    assert ".step" not in payload["messages"][1]["content"]
    restored, source = planning.load_intent(output, brief, manifest)
    assert restored == result
    assert source == "local_ollama:local-test-model"
    changed = tmp_path / "changed.md"
    changed.write_text(brief.read_text() + "\nRequire waterproofing.")
    with pytest.raises(DesignError, match="changed"):
        planning.load_intent(output, changed, manifest)


@pytest.mark.parametrize("failure", ["unsupported", "nonfinite", "code", "unknown"])
def test_invalid_model_output_is_rejected_without_plan(
    monkeypatch, planner_response, tmp_path, failure
):
    response = copy.deepcopy(planner_response)
    if failure == "unsupported":
        response["unresolved_requirements"] = ["Snap-fit not supported"]
    elif failure == "nonfinite":
        response["openings"][0]["offset"] = float("nan")
    elif failure == "code":
        response = "__import__('os').system('do not execute')"
    else:
        response["made_up_feature"] = True
    mock_local_model(monkeypatch, response)
    output = tmp_path / "rejected.json"
    with pytest.raises(DesignError):
        planning.plan(
            EXAMPLES / "enclosure-brief.md",
            EXAMPLES / "components.yaml",
            "local-test-model",
            output,
        )
    assert not output.exists()


def test_unavailable_model_has_explicit_error_and_no_fallback(monkeypatch, tmp_path):
    class Unavailable:
        def open(self, request, timeout):
            raise URLError("model unavailable")

    monkeypatch.setattr(planning, "build_opener", lambda *handlers: Unavailable())
    output = tmp_path / "absent.json"
    with pytest.raises(DesignError, match="Local Ollama request failed"):
        planning.plan(
            EXAMPLES / "enclosure-brief.md",
            EXAMPLES / "components.yaml",
            "not-installed",
            output,
        )
    assert not output.exists()
