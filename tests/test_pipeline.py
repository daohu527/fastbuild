import json
import math
from dataclasses import replace
from pathlib import Path

import cadquery as cq
import pytest
import yaml
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from fastbuild.config import DesignError, Keepout, load_design
from fastbuild.pipeline import _check_export, build, create_model


def test_build_step_roundtrip_and_reproducible_snapshot(demo_config, tmp_path):
    output = tmp_path / "output"
    report = build(demo_config, output)
    assert report["status"] == "geometry_passed"
    assert "loads_materials_and_fea" in report["not_checked"]
    assert report["mounting"]["hole_count"] == 8
    assembly = cq.importers.importStep(str(output / "assembly.step")).val()
    assert len(assembly.Solids()) == 3
    assert assembly.isValid()
    assert math.isclose(
        assembly.Volume(),
        sum(part["volume_mm3"] for part in report["parts"].values()),
        rel_tol=1e-7,
    )
    saved = json.loads((output / "validation.json").read_text())
    assert saved["checks"][-1] == "step_roundtrip_solid_count_volume_and_positions"
    restored = cq.Assembly.load(str(output / "assembly.step"))
    assert {part.name for part in restored.children} == {
        "mounting_plate",
        "controller",
        "sensor",
    }
    assert load_design(output / "design.yaml") == load_design(demo_config)
    plate = cq.importers.importStep(str(output / "mounting_plate.step")).val()
    # Verify a rotated hole at a world-space coordinate, not just solid volume.
    probe = cq.Solid.makeCylinder(1.6, 14, cq.Vector(35, -10, 0))
    assert plate.intersect(probe).Volume() < 1e-5
    with pytest.raises(DesignError, match="already exists"):
        build(demo_config, output)


def test_roundtrip_rejects_lost_placement_even_when_volume_is_equal(tmp_path):
    part = cq.Workplane("XY").box(10, 10, 10).val()
    path = tmp_path / "misplaced.step"
    cq.exporters.export(part.translate((20, 0, 0)), str(path))
    with pytest.raises(DesignError, match="positions"):
        _check_export(path, [part])


def test_arbitrary_rotation_keeps_post_holes_aligned(demo_config):
    design = load_design(demo_config)
    device = replace(design.devices[1], rotation_deg=37)
    model = create_model(replace(design, devices=(device,)))
    angle = math.radians(37)
    x, y = device.mounts[0]
    center = cq.Vector(
        device.position[0] + x * math.cos(angle) - y * math.sin(angle),
        device.position[1] + x * math.sin(angle) + y * math.cos(angle),
        0,
    )
    bore = cq.Solid.makeCylinder(1.6, 14, center)
    assert model.plate.intersect(bore).Volume() < 1e-5


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"position": (-25, 0), "rotation_deg": 0, "standoff": 8}, "collision"),
        ({"position": (11, 0), "rotation_deg": 0, "standoff": 8}, "clearance"),
        ({"position": (60, 0)}, "edge margin"),
        ({"standoff": 0.5}, "standoff"),
        ({"mount_hole_diameter": 3.0}, "too small"),
        ({"mounts": ((0, 0), (10, 0))}, "through hole"),
        ({"mounts": ((80, 80), (90, 90))}, "support landing"),
    ],
)
def test_invalid_geometry_is_rejected(demo_config, change, message):
    design = load_design(demo_config)
    second = replace(design.devices[1], **change)
    with pytest.raises(DesignError, match=message):
        create_model(replace(design, devices=(design.devices[0], second)))


def test_keepouts_rotate_with_device_and_block_other_parts(demo_config):
    design = load_design(demo_config)
    first, second = design.devices
    # Sensor rotates 90 degrees: this local box covers the controller in world space.
    box = Keepout("plug", (-10, 40, 0), (10, 60, 4))
    second = replace(second, keepouts=(box,))
    with pytest.raises(DesignError, match="Keepout intrusion"):
        create_model(replace(design, devices=(first, second)))


def test_post_wall_is_checked(demo_config):
    design = load_design(demo_config)
    with pytest.raises(DesignError, match="post wall"):
        create_model(
            replace(design, mounting=replace(design.mounting, post_diameter=4))
        )


def test_post_collision_with_another_device_is_rejected(demo_config):
    design = load_design(demo_config)
    first, second = design.devices
    second = replace(second, position=(-25, 0), standoff=20)
    with pytest.raises(DesignError, match="posts too close|Post collision"):
        create_model(replace(design, devices=(first, second)))


def test_mounting_bottom_must_be_declared_plane(demo_config, tmp_path):
    design = load_design(demo_config)
    device = design.devices[0]
    shifted = cq.importers.importStep(str(device.step)).translate((0, 0, 1))
    path = tmp_path / "shifted.step"
    cq.exporters.export(shifted, str(path))
    with pytest.raises(DesignError, match="local Z=0"):
        create_model(replace(design, devices=(replace(device, step=path),)))


def test_multiroot_step_must_not_silently_drop_extra_parts(demo_config, tmp_path):
    design = load_design(demo_config)
    device = design.devices[0]
    original = cq.importers.importStep(str(device.step)).val()
    extra = cq.Workplane("XY").box(2, 2, 2).translate((100, 0, 0)).val()
    writer = STEPControl_Writer()
    writer.Transfer(original.wrapped, STEPControl_AsIs)
    writer.Transfer(extra.wrapped, STEPControl_AsIs)
    path = tmp_path / "multiroot.step"
    writer.Write(str(path))
    with pytest.raises(DesignError, match="one valid"):
        create_model(replace(design, devices=(replace(device, step=path),)))


def test_failed_build_does_not_publish_success(demo_config, tmp_path):
    data = yaml.safe_load(demo_config.read_text())
    for device in data["devices"]:
        device["step"] = str(demo_config.parent / device["step"])
    data["devices"][1]["position"] = [-25, 0]
    data["devices"][1]["standoff"] = 8
    config = tmp_path / "collision.yaml"
    config.write_text(yaml.safe_dump(data))
    output = tmp_path / "output"
    with pytest.raises(DesignError, match="collision"):
        build(config, output)
    assert not output.exists()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("units", "inch", "Invalid configuration"),
        ("clearance", float("nan"), "finite"),
        ("clearance", float("inf"), "finite"),
        ("unknown", 1, "Invalid configuration"),
    ],
)
def test_invalid_config(demo_config, tmp_path, key, value, message):
    data = yaml.safe_load(demo_config.read_text())
    data[key] = value
    config = tmp_path / "invalid.yaml"
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(DesignError, match=message):
        load_design(config)


def test_duplicate_names_and_yaml_keys(demo_config, tmp_path):
    data = yaml.safe_load(demo_config.read_text())
    data["devices"][1]["name"] = data["devices"][0]["name"]
    config = tmp_path / "duplicate.yaml"
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(DesignError, match="unique"):
        load_design(config)
    config.write_text("version: 1\nversion: 2\n")
    with pytest.raises(DesignError, match="Duplicate configuration key"):
        load_design(config)


def test_missing_step_is_explicit(demo_config):
    design = load_design(demo_config)
    device = replace(design.devices[0], step=Path("/nonexistent/part.step"))
    with pytest.raises(DesignError, match="does not exist"):
        create_model(replace(design, devices=(device,)))
