from __future__ import annotations

import html
import json
import math
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path

import cadquery as cq
import yaml
from cq_warehouse.fastener import HexHeadScrew, HexNut

from .config import (
    Design,
    DesignError,
    parse_design,
    read_document,
    validate_document,
)
from .pipeline import (
    LENGTH_TOL,
    VOLUME_TOL,
    _check_export,
    _import_step,
    _overlap,
    _valid_solid,
    _world_point,
    create_model,
)
from .planning import check_intent, digest, load_intent


@dataclass
class EnclosureModel:
    parts: dict[str, cq.Shape]
    assembly: cq.Assembly
    exploded: cq.Assembly
    design: Design
    report: dict
    bom: list[dict]


def _box(width: float, depth: float, height: float, z: float = 0) -> cq.Shape:
    return (
        cq.Workplane("XY")
        .box(width, depth, height, centered=(True, True, False))
        .translate((0, 0, z))
        .val()
    )


def _fuse(first: cq.Shape, second: cq.Shape) -> cq.Shape:
    return first.fuse(second).clean()


def _automatic_layout(components: Path, intent: dict) -> Design:
    manifest = read_document(components)
    validate_document(manifest, "components.schema.json")
    data = {
        "version": 1,
        "units": "mm",
        "plate": {
            "width": 1000,
            "depth": 1000,
            "thickness": intent["base"],
            "edge_margin": intent["clearance"],
        },
        "mounting": {
            "size": "M3-0.5",
            "fit": "Normal",
            "post_diameter": 8,
            "min_wall": 1.5,
        },
        "clearance": intent["clearance"],
        "devices": [
            {
                **item,
                "position": [0, 0],
                "rotation_deg": item.get("rotation_deg", 0),
                "standoff": intent["standoff"],
            }
            for item in manifest["devices"]
        ],
    }
    design = parse_design(data, components.resolve().parent)
    if any(
        device.name in {"body", "lid"} or device.name.startswith(("lid_", "device_"))
        for device in design.devices
    ):
        raise DesignError("Names body/lid and prefixes lid_/device_ are reserved")
    envelopes = []
    for device in design.devices:
        shape = _import_step(device.step)
        _valid_solid(shape, device.name)
        rotated = shape.rotate((0, 0, 0), (0, 0, 1), device.rotation_deg)
        bounds = rotated.BoundingBox()
        xs, ys = [bounds.xmin, bounds.xmax], [bounds.ymin, bounds.ymax]
        for box in device.keepouts:
            for x in (box.minimum[0], box.maximum[0]):
                for y in (box.minimum[1], box.maximum[1]):
                    px, py = _world_point((x, y), device)
                    xs.append(px)
                    ys.append(py)
        # Reserve a simple vertical tool envelope at each nut location.
        for point in device.mounts:
            x, y = _world_point(point, device)
            xs.extend((x - 6, x + 6))
            ys.extend((y - 6, y + 6))
        envelopes.append((min(xs), min(ys), max(xs), max(ys)))
    axis = 0 if intent["layout_axis"] == "x" else 1
    total = sum(box[axis + 2] - box[axis] for box in envelopes)
    total += intent["gap"] * (len(envelopes) - 1)
    cursor = -total / 2
    devices = []
    for device, box in zip(design.devices, envelopes):
        size = box[axis + 2] - box[axis]
        position = [-(box[0] + box[2]) / 2, -(box[1] + box[3]) / 2]
        position[axis] = cursor - box[axis]
        devices.append(replace(device, position=tuple(position)))
        cursor += size + intent["gap"]
    cross = max(box[3 - axis] - box[1 - axis] for box in envelopes)
    dimensions = [total, cross] if axis == 0 else [cross, total]
    return replace(
        design,
        devices=tuple(devices),
        plate=replace(
            design.plate,
            width=max(44, dimensions[0] + 2 * intent["clearance"]),
            depth=max(36, dimensions[1] + 2 * intent["clearance"]),
        ),
    )


def _upstream_shell(width: float, depth: float, height: float, wall: float) -> cq.Shape:
    try:
        from cq_enclosure_builder import Enclosure, EnclosureSize
    except FileNotFoundError as exc:
        raise DesignError(
            "cq_enclosure_builder requires its official CSV setup step; run "
            "'<venv>/bin/python tools/prepare_enclosure.py'"
        ) from exc
    upstream = Enclosure(
        EnclosureSize(width, depth, height, wall),
        add_corner_lid_screws=False,
        add_lid_support=False,
        add_top_support=False,
        no_fillet_top=True,
        no_fillet_bottom=True,
        lid_panel_add_chamfer=False,
    )
    upstream.assemble()
    # The upstream printable box is already oriented with its open side upward.
    panels = upstream.printables["box"][0].Solids()
    shell = cq.Workplane("XY").newObject(panels).combine().val()
    _valid_solid(shell, "upstream enclosure shell")
    return shell


def _openings(body: cq.Shape, width: float, depth: float, height: float, intent: dict):
    wall = intent["wall"]
    cuts: list[cq.Shape] = []
    for opening in intent["openings"]:
        along_x = opening["face"] in {"front", "back"}
        available = width if along_x else depth
        span = opening["width"] + (opening["count"] - 1) * opening["pitch"]
        if (
            abs(opening["offset"]) + span / 2 > available / 2 - wall - 2
            or opening["z"] - opening["height"] / 2 < intent["base"] + 2
            or opening["z"] + opening["height"] / 2 > height - 2
            or (along_x and opening["z"] + opening["height"] / 2 > height - 9)
        ):
            raise DesignError(f"Opening exceeds safe wall region: {opening['name']}")
        if opening["count"] > 1 and opening["pitch"] < opening["width"] + 2:
            raise DesignError(f"Opening web must be >= 2 mm: {opening['name']}")
        for index in range(opening["count"]):
            offset = (
                opening["offset"]
                + (index - (opening["count"] - 1) / 2) * opening["pitch"]
            )
            side = -1 if opening["face"] in {"front", "left"} else 1
            if along_x:
                cut = _box(
                    opening["width"],
                    wall + 2,
                    opening["height"],
                    opening["z"] - opening["height"] / 2,
                ).translate((offset, side * (depth / 2 - wall / 2), 0))
            else:
                cut = _box(
                    wall + 2,
                    opening["width"],
                    opening["height"],
                    opening["z"] - opening["height"] / 2,
                ).translate((side * (width / 2 - wall / 2), offset, 0))
            if any(previous.distance(cut) < 2 - LENGTH_TOL for previous in cuts):
                raise DesignError("Openings overlap or leave less than 2 mm web")
            if _overlap(body, cut) <= VOLUME_TOL:
                raise DesignError(f"Opening did not cut the shell: {opening['name']}")
            cuts.append(cut)
            body = body.cut(cut)
    return body.clean()


def create_enclosure(components: Path, intent: dict) -> EnclosureModel:
    check_intent(intent)
    design = _automatic_layout(components, intent)
    foundation = create_model(design)
    width = design.plate.width + 2 * intent["wall"]
    depth = design.plate.depth + 2 * intent["wall"]
    top = max(shape.BoundingBox().zmax for shape in foundation.devices.values())
    for device in design.devices:
        for box in device.keepouts:
            top = max(top, intent["base"] + intent["standoff"] + box.maximum[2])
    height = top + intent["headroom"]
    body = _upstream_shell(width, depth, height, intent["wall"])
    body = _fuse(body, foundation.plate)
    lid = _box(width, depth, intent["lid"], height)
    lip_outer = _box(design.plate.width - 0.6, design.plate.depth - 0.6, 2, height - 2)
    lip_inner = _box(design.plate.width - 3.6, design.plate.depth - 3.6, 2, height - 2)
    lid = _fuse(lid, lip_outer.cut(lip_inner))
    fasteners: dict[str, cq.Shape] = {}
    connections = []
    nut = HexNut("M3-0.5", "iso4032", simple=True)
    sample = HexHeadScrew("M3-0.5", 10, "iso4017", simple=True)
    pocket_depth = sample.screw_data["k"] + 0.3
    if intent["base"] - pocket_depth < 1.5:
        raise DesignError("Base leaves less than 1.5 mm above bolt-head pockets")

    def attach(
        name: str, x: float, y: float, bottom: float, nut_z: float, owner: str
    ) -> cq.Shape:
        seat = bottom + pocket_depth
        required = nut_z + nut.nut_thickness + 0.5 - seat
        lengths = [length for length in sample.nominal_lengths if length >= required]
        if not lengths:
            raise DesignError(f"No supported ISO 4017 screw length for {name}")
        length = min(lengths)
        screw = HexHeadScrew("M3-0.5", length, "iso4017", simple=True)
        bolt_shape = cq.Shape.cast(screw.wrapped)
        bolt_shape = bolt_shape.rotate((0, 0, 0), (1, 0, 0), 180)
        fasteners[f"{name}_bolt"] = bolt_shape.translate((x, y, seat))
        fasteners[f"{name}_nut"] = cq.Shape.cast(nut.wrapped).translate((x, y, nut_z))
        connections.append(
            {
                "name": name,
                "owner": owner,
                "xy_mm": [x, y],
                "bolt": f"ISO4017 M3-0.5x{length:g}",
                "nut": "ISO4032 M3-0.5",
                "nut_z_mm": nut_z,
                "bolt_tip_z_mm": seat + length,
                "head_pocket_depth_mm": pocket_depth,
            }
        )
        diameter = screw.screw_data["s"] / math.cos(math.pi / 6) + 0.4
        return (
            cq.Workplane("XY")
            .polygon(6, diameter)
            .extrude(pocket_depth)
            .translate((x, y, bottom))
            .val()
        )

    for device in design.devices:
        shape = foundation.devices[device.name]
        nut_z = shape.BoundingBox().zmax
        for index, point in enumerate(device.mounts):
            x, y = _world_point(point, device)
            # Nut must sit on a flat top surface, not float over a tall component.
            seat = cq.Solid.makeCylinder(
                nut.nut_diameter / 2, 0.1, cq.Vector(x, y, nut_z - 0.1)
            ).cut(
                cq.Solid.makeCylinder(
                    device.mount_hole_diameter / 2, 0.1, cq.Vector(x, y, nut_z - 0.1)
                )
            )
            if seat.cut(shape).Volume() > VOLUME_TOL:
                raise DesignError(f"{device.name}: nut needs a flat top mounting seat")
            pocket = attach(
                f"device_{device.name}_{index}", x, y, 0, nut_z, device.name
            )
            bore = cq.Solid.makeCylinder(1.7, nut_z, cq.Vector(x, y, 0))
            body = body.cut(pocket).cut(bore)

    for index, (x, y) in enumerate(
        (x, y)
        for x in (-width / 2 + 11, width / 2 - 11)
        for y in (-depth / 2 - 4, depth / 2 + 4)
    ):
        tab = (
            _box(18, 18, 7, height - 7)
            .translate((x, y, 0))
            .cut(_box(design.plate.width, design.plate.depth, 7, height - 7))
        )
        body = _fuse(body, tab)
        lid = _fuse(lid, _box(18, 18, intent["lid"], height).translate((x, y, 0)))
        bore = cq.Solid.makeCylinder(
            1.7, 7 + intent["lid"], cq.Vector(x, y, height - 7)
        )
        pocket = attach(f"lid_{index}", x, y, height - 7, height + intent["lid"], "lid")
        body = body.cut(bore).cut(pocket)
        lid = lid.cut(bore)
    body, lid = _openings(body, width, depth, height, intent), lid.clean()
    parts = {"body": body, "lid": lid, **foundation.devices, **fasteners}
    for name, shape in parts.items():
        _valid_solid(shape, name)
    for (name_a, first), (name_b, second) in combinations(parts.items(), 2):
        if _overlap(first, second) > VOLUME_TOL:
            raise DesignError(f"Enclosure collision: {name_a} / {name_b}")
    for device in design.devices:
        for box in device.keepouts:
            size = tuple(high - low for low, high in zip(box.minimum, box.maximum))
            shape = cq.Workplane("XY").box(*size, centered=False).val()
            shape = shape.translate(box.minimum)
            shape = shape.rotate((0, 0, 0), (0, 0, 1), device.rotation_deg)
            shape = shape.translate(
                (*device.position, intent["base"] + device.standoff)
            )
            for name, other in parts.items():
                if name != device.name and _overlap(shape, other) > VOLUME_TOL:
                    raise DesignError(
                        f"Keepout intrusion: {device.name}/{box.name}/{name}"
                    )
    for connection in connections:
        if connection["owner"] != "lid":
            x, y = connection["xy_mm"]
            z = connection["nut_z_mm"] + nut.nut_thickness
            tool = cq.Solid.makeCylinder(6, height - z, cq.Vector(x, y, z))
            for name, shape in {"body": body, **foundation.devices}.items():
                if _overlap(tool, shape) > VOLUME_TOL:
                    raise DesignError(f"Vertical nut-tool access blocked: {name}")
    envelope = cq.Compound.makeCompound(list(parts.values())).BoundingBox()
    dimensions = [envelope.xlen, envelope.ylen, envelope.zlen]
    if any(
        size > limit + LENGTH_TOL for size, limit in zip(dimensions, intent["max_size"])
    ):
        raise DesignError(f"Enclosure exceeds max_size: {dimensions}")
    assembly, exploded = cq.Assembly(name="enclosure"), cq.Assembly(name="exploded")
    for name, shape in parts.items():
        color = "gray" if name == "body" else "orange" if name == "lid" else "blue"
        if name in fasteners:
            color = "lightgray"
        assembly.add(shape, name=name, color=cq.Color(color))
        shift = height + 10 if name == "lid" or name.startswith("lid_") else 0
        exploded.add(shape.translate((0, 0, shift)), name=name, color=cq.Color(color))
    quantities = Counter(connection["bolt"] for connection in connections)
    quantities["ISO4032 M3-0.5"] = len(connections)
    bom = [
        {"item": name, "quantity": count} for name, count in sorted(quantities.items())
    ]
    bom += [{"item": "FDM body", "quantity": 1}, {"item": "FDM lid", "quantity": 1}]
    report = {
        "status": "geometry_passed",
        "template": intent["template"],
        "units": "mm",
        "dimensions_mm": dimensions,
        "checks": foundation.report["checks"]
        + [
            "closed_enclosure_part_collisions_including_fasteners",
            "flat_nut_seats_and_standard_bolt_length",
            "declared_keepouts_against_shell_lid_and_fasteners",
            "vertical_device_nut_tool_envelope_6mm_radius",
            "opening_edge_margins_and_webs",
            "final_maximum_envelope_including_tabs_and_fasteners",
        ],
        "not_checked": [
            "arbitrary_natural_language_requirement_satisfaction",
            "complete_assembly_and_removal_paths",
            "fastener_torque_preload_and_thread_strength",
            "fdm_tolerances_supports_and_strength",
            "thermal_waterproof_emc_and_fea",
            "freecad_runtime_and_native_feature_tree",
        ],
        "inputs": foundation.report["inputs"],
        "connections": connections,
        "fixation": "ISO4017 bolts, underside hex head pockets, ISO4032 top nuts",
        "assumptions": [
            "M3 fasteners and simple (unmodeled) threads",
            "Device bottom and top mounting seats must be planar",
            "Hex pockets use 0.4 mm diametral prototype clearance",
            "Top lid has four external ears and a 0.3 mm radial-clearance skirt",
            "No connector opening or connector mating is inferred from keepouts",
        ],
        "parts": {
            name: {"volume_mm3": shape.Volume()} for name, shape in parts.items()
        },
        "versions": {
            "cadquery": cq.__version__,
            "cq_enclosure_builder_commit": "264af08306a96b1a8c0db342f96d479a8b433d70",
            "cq_warehouse_commit": "a57bb7a24762d367d66369f280a24c38a64e73dd",
        },
    }
    return EnclosureModel(parts, assembly, exploded, design, report, bom)


def enclose(brief: Path, components: Path, intent_path: Path, output: Path) -> dict:
    if output.exists():
        raise DesignError(f"Output directory already exists: {output}")
    text = brief.read_text(encoding="utf-8")
    if not text.strip():
        raise DesignError("Design brief is empty")
    intent, source = load_intent(intent_path, brief, components)
    model = create_enclosure(components, intent)
    output.mkdir(parents=True, exist_ok=False)
    for name in ("body", "lid"):
        cq.exporters.export(model.parts[name], str(output / f"{name}.step"))
        _check_export(output / f"{name}.step", [model.parts[name]])
    model.assembly.export(str(output / "assembly.step"))
    _check_export(output / "assembly.step", list(model.parts.values()))
    model.exploded.export(str(output / "exploded.step"))
    _check_export(
        output / "exploded.step",
        [part.obj for part in model.exploded.children],
    )
    cq.exporters.export(model.assembly.toCompound(), str(output / "assembly.svg"))
    cq.exporters.export(model.exploded.toCompound(), str(output / "exploded.svg"))
    model.report["checks"].append("step_roundtrip_solid_count_volume_and_positions")
    model.report.update(
        intent_source=source,
        brief_sha256=digest(brief),
        components_sha256=digest(components),
        interpreted_requirements=intent["interpreted_requirements"],
    )
    (output / "brief.md").write_text(text, encoding="utf-8")
    (output / "intent.json").write_text(
        json.dumps(intent, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "resolved-layout.yaml").write_text(
        yaml.safe_dump(
            json.loads(json.dumps(model.design.snapshot())), sort_keys=False
        ),
        encoding="utf-8",
    )
    manifest = read_document(components)
    for item in manifest["devices"]:
        item["step"] = str((components.resolve().parent / item["step"]).resolve())
    (output / "components.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (output / "bom.json").write_text(
        json.dumps(model.bom, indent=2) + "\n", encoding="utf-8"
    )
    rows = "".join(
        f"<tr><td>{html.escape(item['item'])}</td><td>{item['quantity']}</td></tr>"
        for item in model.bom
    )
    (output / "preview.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Fastbuild enclosure</title>'
        "<h1>Enclosure prototype / 外壳原型</h1>"
        "<p>Geometry checks only. Not manufacturing or strength approval.</p>"
        '<h2>Assembly</h2><img src="assembly.svg" alt="assembly">'
        '<h2>Lid removed</h2><img src="exploded.svg" alt="exploded">'
        f"<h2>Fasteners / BOM</h2><table>{rows}</table>",
        encoding="utf-8",
    )
    (output / "validation.json").write_text(
        json.dumps(model.report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return model.report
