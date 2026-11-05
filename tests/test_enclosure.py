import copy
import json
from pathlib import Path

import cadquery as cq
import pytest
import yaml

from fastbuild.config import DesignError, read_document
from fastbuild.enclosure import _automatic_layout, _box, _openings, enclose
from fastbuild.planning import check_intent

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def intent():
    return read_document(EXAMPLES / "enclosure-intent.json")


@pytest.fixture(scope="session")
def components(demo_config, tmp_path_factory):
    root = tmp_path_factory.mktemp("enclosure")
    data = read_document(demo_config)
    devices = []
    for device in data["devices"]:
        devices.append(
            {
                key: value
                for key, value in {
                    **device,
                    "step": str(demo_config.parent / device["step"]),
                }.items()
                if key not in {"position", "standoff"}
            }
        )
    path = root / "components.yaml"
    path.write_text(yaml.safe_dump({"units": "mm", "devices": devices}))
    return path


@pytest.fixture(scope="module")
def enclosure_result(components, tmp_path_factory):
    root = tmp_path_factory.mktemp("enclosure_result")
    output = root / "result"
    report = enclose(
        EXAMPLES / "enclosure-brief.md",
        components,
        EXAMPLES / "enclosure-intent.json",
        output,
    )
    return output, report


def test_auto_layout_without_user_positions(components, intent):
    design = _automatic_layout(components, check_intent(intent))
    assert design.devices[0].position[0] < design.devices[1].position[0]
    assert design.devices[0].position[1] != 0  # Offset keepout is accounted for.
    intent["layout_axis"] = "y"
    vertical = _automatic_layout(components, check_intent(intent))
    assert vertical.devices[0].position[1] < vertical.devices[1].position[1]
    assert vertical.plate.depth > design.plate.depth


def test_complete_enclosure_exports_fasteners_and_preview(enclosure_result):
    output, report = enclosure_result
    assembly = cq.Assembly.load(str(output / "assembly.step"))
    names = {part.name for part in assembly.children}
    assert {"body", "lid", "controller", "sensor"} <= names
    assert len(assembly.toCompound().Solids()) == 28
    assert len(report["connections"]) == 12
    assert report["dimensions_mm"][1] > 70  # Includes the external lid ears.
    assert report["intent_source"] == "reviewed_intent_file_not_automatic_text_parsing"
    assert "thermal_waterproof_emc_and_fea" in report["not_checked"]
    assert (output / "preview.html").is_file()
    assert "<svg" in (output / "exploded.svg").read_text()
    exploded = cq.importers.importStep(str(output / "exploded.step")).val()
    assert exploded.BoundingBox().zlen > assembly.toCompound().BoundingBox().zlen
    bom = json.loads((output / "bom.json").read_text())
    assert sum(item["quantity"] for item in bom if item["item"].startswith("ISO")) == 24
    layout = read_document(output / "resolved-layout.yaml")
    assert all("position" in item for item in layout["devices"])
    snapshot = read_document(output / "components.yaml")
    assert all(
        Path(item["step"]).is_absolute() and "position" not in item
        for item in snapshot["devices"]
    )


def test_base_holes_and_hex_pockets_are_not_filled_by_upstream_floor(enclosure_result):
    output, report = enclosure_result
    body = cq.importers.importStep(str(output / "body.step")).val()
    for connection in report["connections"]:
        if connection["owner"] == "lid":
            continue
        x, y = connection["xy_mm"]
        bore = cq.Solid.makeCylinder(1.6, 14, cq.Vector(x, y, 0))
        assert body.intersect(bore).Volume() < 1e-5
        head_probe = cq.Solid.makeCylinder(2.5, 2.4, cq.Vector(x, y, 0))
        assert body.intersect(head_probe).Volume() < 1e-5


def test_front_vent_is_really_open(enclosure_result):
    output, _ = enclosure_result
    body = cq.importers.importStep(str(output / "body.step")).val()
    # Example shell inner Y is +/-25.5; outer Y is +/-28.5.
    for x in (-12, -6, 0, 6, 12):
        probe = _box(2.9, 3.2, 5.9, 13.05).translate((x, -27, 0))
        assert body.intersect(probe).Volume() < 1e-5


@pytest.mark.parametrize(
    "change",
    [
        {"width": 1000},
        {"z": 4},
        {"pitch": 3},
        {"z": 30},
    ],
)
def test_unsafe_openings_fail_instead_of_being_ignored(intent, change):
    opening = copy.deepcopy(intent["openings"][0])
    opening.update(change)
    intent["openings"] = [opening]
    with pytest.raises(DesignError, match="Opening"):
        _openings(_box(90, 57, 34), 90, 57, 34, intent)


def test_unresolved_and_unsupported_designs_are_not_built(intent):
    intent["unresolved_requirements"] = ["IP67 sealing not implemented"]
    with pytest.raises(DesignError, match="IP67"):
        check_intent(intent)
    intent["unresolved_requirements"] = []
    intent["fixation"] = "snap_fit"
    with pytest.raises(DesignError, match="Invalid configuration"):
        check_intent(intent)


def test_maximum_size_failure_does_not_publish_success(components, intent, tmp_path):
    intent["max_size"] = [50, 50, 30]
    path = tmp_path / "small.json"
    path.write_text(json.dumps(intent))
    output = tmp_path / "result"
    with pytest.raises(DesignError, match="max_size"):
        enclose(EXAMPLES / "enclosure-brief.md", components, path, output)
    assert not output.exists()
