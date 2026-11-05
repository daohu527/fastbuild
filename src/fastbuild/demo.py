from __future__ import annotations

from pathlib import Path

import cadquery as cq
import yaml

from .config import DesignError
from .pipeline import build


def create_demo_inputs(output: Path) -> Path:
    if output.exists():
        raise DesignError(f"Output directory already exists: {output}")
    output.mkdir(parents=True)
    controller_mounts = [[-14, -10], [14, -10], [-14, 10], [14, 10]]
    sensor_mounts = [[-10, -8], [10, -8], [-10, 8], [10, 8]]
    for name, dimensions, mounts in [
        ("controller", (40, 30, 3), controller_mounts),
        ("sensor", (30, 26, 4), sensor_mounts),
    ]:
        shape = (
            cq.Workplane("XY")
            .box(*dimensions, centered=(True, True, False))
            .faces(">Z")
            .workplane()
            .pushPoints(mounts)
            .hole(3.4)
        )
        cq.exporters.export(shape, str(output / f"{name}.step"))
    design = {
        "version": 1,
        "units": "mm",
        "plate": {"width": 120, "depth": 80, "thickness": 4, "edge_margin": 3},
        "mounting": {
            "size": "M3-0.5",
            "fit": "Normal",
            "post_diameter": 8,
            "min_wall": 1.5,
        },
        "clearance": 2,
        "devices": [
            {
                "name": "controller",
                "step": "controller.step",
                "position": [-25, 0],
                "rotation_deg": 0,
                "standoff": 8,
                "mount_hole_diameter": 3.4,
                "mounts": controller_mounts,
                "keepouts": [
                    {"name": "connector", "min": [-8, 15, 0], "max": [8, 27, 8]}
                ],
            },
            {
                "name": "sensor",
                "step": "sensor.step",
                "position": [27, 0],
                "rotation_deg": 90,
                "standoff": 10,
                "mount_hole_diameter": 3.4,
                "mounts": sensor_mounts,
            },
        ],
    }
    config = output / "design.yaml"
    config.write_text(yaml.safe_dump(design, sort_keys=False), encoding="utf-8")
    return config


def demo(output: Path) -> dict:
    if output.exists():
        raise DesignError(f"Output directory already exists: {output}")
    config = create_demo_inputs(output / "inputs")
    return build(config, output / "result")
