from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import cadquery as cq
import yaml
from cq_warehouse.fastener import SocketHeadCapScrew

from .config import Design, DesignError, Device, load_design

LENGTH_TOL = 1e-5
VOLUME_TOL = 1e-5

NOT_CHECKED = [
    "assembly_and_removal_paths",
    "fastener_connection_and_tool_access",
    "manufacturing_process_and_print_tolerances",
    "loads_materials_and_fea",
    "native_freecad_feature_tree",
]


@dataclass
class Model:
    assembly: cq.Assembly
    plate: cq.Shape
    devices: dict[str, cq.Shape]
    report: dict


def _import_step(path: Path) -> cq.Shape:
    shapes = cq.importers.importStep(str(path)).vals()
    if not shapes:
        raise DesignError(f"STEP input contains no shapes: {path}")
    return cq.Compound.makeCompound(shapes)


def _valid_solid(shape: cq.Shape, label: str) -> None:
    if not shape.isValid() or len(shape.Solids()) != 1 or shape.Volume() <= VOLUME_TOL:
        raise DesignError(f"{label}: expected one valid, non-empty solid")


def _overlap(first: cq.Shape, second: cq.Shape) -> float:
    return abs(first.intersect(second).Volume())


def _place(shape: cq.Shape, device: Device, plate_thickness: float) -> cq.Shape:
    return shape.rotate((0, 0, 0), (0, 0, 1), device.rotation_deg).translate(
        (*device.position, plate_thickness + device.standoff)
    )


def _world_point(point: tuple[float, float], device: Device) -> tuple[float, float]:
    angle = math.radians(device.rotation_deg)
    x, y = point
    return (
        device.position[0] + x * math.cos(angle) - y * math.sin(angle),
        device.position[1] + x * math.sin(angle) + y * math.cos(angle),
    )


def _cylinder(x: float, y: float, z: float, radius: float, height: float) -> cq.Shape:
    return cq.Solid.makeCylinder(radius, height, cq.Vector(x, y, z))


def _check_mounts(
    shape: cq.Shape, device: Device, post_radius: float, hole_diameter: float
) -> None:
    bounds = shape.BoundingBox()
    if abs(bounds.zmin) > LENGTH_TOL:
        raise DesignError(f"{device.name}: mounting bottom must be at local Z=0")
    if device.mount_hole_diameter + LENGTH_TOL < hole_diameter:
        raise DesignError(
            f"{device.name}: mounting holes are too small for selected fit"
        )
    if device.mount_hole_diameter / 2 + LENGTH_TOL >= post_radius:
        raise DesignError(f"{device.name}: mounting hole must be smaller than the post")
    for x, y in device.mounts:
        bore = _cylinder(
            x,
            y,
            -LENGTH_TOL,
            device.mount_hole_diameter / 2 - LENGTH_TOL,
            bounds.zlen + 2 * LENGTH_TOL,
        )
        if _overlap(shape, bore) > VOLUME_TOL:
            raise DesignError(f"{device.name}: no declared through hole at {(x, y)}")
        # A thin annulus verifies a flat landing, not just an empty hole in space.
        height = min(0.1, bounds.zlen / 2)
        pad = _cylinder(x, y, 0, post_radius, height).cut(
            _cylinder(x, y, 0, device.mount_hole_diameter / 2, height)
        )
        if pad.cut(shape).Volume() > VOLUME_TOL:
            raise DesignError(f"{device.name}: incomplete support landing at {(x, y)}")


def create_model(design: Design) -> Model:
    plate = design.plate
    mounting = design.mounting
    standard = SocketHeadCapScrew(
        size=mounting.size, length=10, fastener_type="iso4762", simple=True
    )
    hole_diameter = standard.clearance_hole_diameters[mounting.fit]
    radius = mounting.post_diameter / 2
    if radius - hole_diameter / 2 + LENGTH_TOL < mounting.min_wall:
        raise DesignError("Installation post wall is below mounting.min_wall")
    if min(plate.width, plate.depth) <= 2 * plate.edge_margin:
        raise DesignError("Plate is too small for the declared edge margin")

    structure = cq.Workplane("XY").box(
        plate.width, plate.depth, plate.thickness, centered=(True, True, False)
    )
    placed = {}
    posts: list[tuple[str, cq.Shape]] = []
    holes = []
    input_records = []
    keepouts: list[tuple[str, str, cq.Shape]] = []
    for device in design.devices:
        if device.standoff + LENGTH_TOL < design.clearance:
            raise DesignError(f"{device.name}: standoff is below minimum clearance")
        if not device.step.is_file():
            raise DesignError(f"{device.name}: STEP file does not exist: {device.step}")
        digest = hashlib.sha256(device.step.read_bytes()).hexdigest()
        shape = _import_step(device.step)
        _valid_solid(shape, device.name)
        _check_mounts(shape, device, radius, hole_diameter)
        world = _place(shape, device, plate.thickness)
        bounds = world.BoundingBox()
        if (
            bounds.xmin < -plate.width / 2 + plate.edge_margin - LENGTH_TOL
            or bounds.xmax > plate.width / 2 - plate.edge_margin + LENGTH_TOL
            or bounds.ymin < -plate.depth / 2 + plate.edge_margin - LENGTH_TOL
            or bounds.ymax > plate.depth / 2 - plate.edge_margin + LENGTH_TOL
        ):
            raise DesignError(f"{device.name}: device exceeds plate edge margin")
        placed[device.name] = world
        input_records.append(
            {"name": device.name, "step": str(device.step), "sha256": digest}
        )
        for point in device.mounts:
            x, y = _world_point(point, device)
            if (
                abs(x) + radius > plate.width / 2 - plate.edge_margin + LENGTH_TOL
                or abs(y) + radius > plate.depth / 2 - plate.edge_margin + LENGTH_TOL
            ):
                raise DesignError(f"{device.name}: post exceeds plate edge margin")
            post = _cylinder(x, y, plate.thickness, radius, device.standoff)
            posts.append((device.name, post))
            holes.append((x, y))
        for box in device.keepouts:
            size = tuple(high - low for low, high in zip(box.minimum, box.maximum))
            local = cq.Workplane("XY").box(*size, centered=False).val()
            local = local.translate(box.minimum)
            keepouts.append(
                (device.name, box.name, _place(local, device, plate.thickness))
            )

    distances = []
    for (name_a, first), (name_b, second) in combinations(placed.items(), 2):
        if _overlap(first, second) > VOLUME_TOL:
            raise DesignError(f"Device collision: {name_a} / {name_b}")
        distance = first.distance(second)
        if distance + LENGTH_TOL < design.clearance:
            raise DesignError(f"Insufficient clearance: {name_a} / {name_b}")
        distances.append({"first": name_a, "second": name_b, "distance_mm": distance})
    for index, (owner, post) in enumerate(posts):
        for other_owner, other in posts[index + 1 :]:
            if post.distance(other) + LENGTH_TOL < design.clearance:
                raise DesignError(
                    f"Installation posts too close: {owner} / {other_owner}"
                )
        for name, device_shape in placed.items():
            if _overlap(post, device_shape) > VOLUME_TOL:
                raise DesignError(f"Post collision: {owner} / {name}")
            if (
                owner != name
                and post.distance(device_shape) + LENGTH_TOL < design.clearance
            ):
                raise DesignError(f"Post clearance too small: {owner} / {name}")
        structure = structure.union(post)
    cutters = (
        cq.Workplane("XY")
        .pushPoints(holes)
        .circle(hole_diameter / 2)
        .extrude(plate.thickness + max(device.standoff for device in design.devices))
    )
    final_plate = structure.cut(cutters).val()
    _valid_solid(final_plate, "mounting_plate")
    for name, shape in placed.items():
        if _overlap(final_plate, shape) > VOLUME_TOL:
            raise DesignError(f"Mounting plate collision: {name}")
    for owner, label, box in keepouts:
        for name, shape in {"mounting_plate": final_plate, **placed}.items():
            if name != owner and _overlap(box, shape) > VOLUME_TOL:
                raise DesignError(f"Keepout intrusion: {owner}/{label} by {name}")

    assembly = cq.Assembly(name="product")
    assembly.add(final_plate, name="mounting_plate", color=cq.Color("gray"))
    for name, shape in placed.items():
        assembly.add(shape, name=name, color=cq.Color("blue"))
    report = {
        "status": "geometry_passed",
        "units": "mm",
        "template": "baseplate_with_clearance_posts",
        "checks": [
            "schema_and_finite_numbers",
            "valid_single_solid_inputs",
            "through_holes_and_flat_support_landings",
            "post_wall_and_plate_edge_margin",
            "device_and_post_collisions_and_clearances",
            "declared_keepout_intrusions",
        ],
        "not_checked": NOT_CHECKED,
        "inputs": input_records,
        "mounting": {
            "standard": "iso4762",
            "size": mounting.size,
            "fit": mounting.fit,
            "clearance_hole_diameter_mm": hole_diameter,
            "hole_count": len(holes),
            "source": "cq_warehouse",
            "connection": "clearance_holes_only_no_fasteners_or_retention",
        },
        "device_distances": distances,
        "parts": {
            name: {
                "volume_mm3": shape.Volume(),
                "bounds_mm": [
                    shape.BoundingBox().xlen,
                    shape.BoundingBox().ylen,
                    shape.BoundingBox().zlen,
                ],
            }
            for name, shape in {"mounting_plate": final_plate, **placed}.items()
        },
    }
    return Model(assembly, final_plate, placed, report)


def _check_export(path: Path, expected: list[cq.Shape]) -> None:
    imported = _import_step(path)
    if not imported.isValid() or len(imported.Solids()) != len(expected):
        raise DesignError(f"STEP round-trip changed validity or solid count: {path}")
    if not math.isclose(
        imported.Volume(),
        sum(shape.Volume() for shape in expected),
        rel_tol=1e-7,
        abs_tol=VOLUME_TOL,
    ):
        raise DesignError(f"STEP round-trip changed volume: {path}")

    def signature(shape: cq.Shape) -> tuple[float, ...]:
        bounds = shape.BoundingBox()
        return (
            bounds.xmin,
            bounds.ymin,
            bounds.zmin,
            bounds.xmax,
            bounds.ymax,
            bounds.zmax,
            shape.Volume(),
        )

    restored = [signature(shape) for shape in imported.Solids()]
    for shape in expected:
        before = signature(shape)
        match = next(
            (
                index
                for index, after in enumerate(restored)
                if all(
                    math.isclose(a, b, rel_tol=1e-7, abs_tol=LENGTH_TOL)
                    for a, b in zip(before, after)
                )
            ),
            None,
        )
        if match is None:
            raise DesignError(
                f"STEP round-trip changed part positions or bounds: {path}"
            )
        restored.pop(match)


def build(config: Path, output: Path) -> dict:
    if output.exists():
        raise DesignError(f"Output directory already exists: {output}")
    design = load_design(config)
    model = create_model(design)
    output.mkdir(parents=True, exist_ok=False)
    cq.exporters.export(model.plate, str(output / "mounting_plate.step"))
    model.assembly.export(str(output / "assembly.step"))
    _check_export(output / "mounting_plate.step", [model.plate])
    _check_export(output / "assembly.step", [model.plate, *model.devices.values()])
    model.report["checks"].append("step_roundtrip_solid_count_volume_and_positions")
    model.report["versions"] = {
        "cadquery": cq.__version__,
        "cq_warehouse_commit": "a57bb7a24762d367d66369f280a24c38a64e73dd",
    }
    # JSON-compatible lists avoid Python tuple tags in the portable YAML snapshot.
    snapshot = json.loads(json.dumps(design.snapshot()))
    (output / "design.yaml").write_text(
        yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8"
    )
    (output / "validation.json").write_text(
        json.dumps(model.report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return model.report
