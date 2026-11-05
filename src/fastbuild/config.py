from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class DesignError(ValueError):
    """A design cannot be built or does not pass its declared checks."""


@dataclass(frozen=True)
class Keepout:
    name: str
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]


@dataclass(frozen=True)
class Device:
    name: str
    step: Path
    position: tuple[float, float]
    rotation_deg: float
    standoff: float
    mount_hole_diameter: float
    mounts: tuple[tuple[float, float], ...]
    keepouts: tuple[Keepout, ...]


@dataclass(frozen=True)
class Plate:
    width: float
    depth: float
    thickness: float
    edge_margin: float


@dataclass(frozen=True)
class Mounting:
    size: str
    fit: str
    post_diameter: float
    min_wall: float


@dataclass(frozen=True)
class Design:
    plate: Plate
    mounting: Mounting
    clearance: float
    devices: tuple[Device, ...]

    def snapshot(self) -> dict:
        from dataclasses import asdict

        data = asdict(self)
        data.update(version=1, units="mm")
        for device in data["devices"]:
            device["step"] = str(device["step"])
            device["keepouts"] = [
                {"name": box["name"], "min": box["minimum"], "max": box["maximum"]}
                for box in device["keepouts"]
            ]
        return data


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict:
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise DesignError("Configuration keys must be strings")
        if key in mapping:
            raise DesignError(f"Duplicate configuration key: {key}")
        mapping[key] = loader.construct_object(value_node)
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _check_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise DesignError("All dimensions and angles must be finite")
    if isinstance(value, dict):
        for child in value.values():
            _check_finite(child)
    if isinstance(value, list):
        for child in value:
            _check_finite(child)


def read_document(path: Path) -> object:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise DesignError(f"Invalid YAML: {exc}") from exc
    _check_finite(data)
    return data


def validate_document(data: object, schema_name: str) -> None:
    _check_finite(data)
    schema = json.loads(files("fastbuild").joinpath(schema_name).read_text("utf-8"))
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "<root>"
        raise DesignError(
            f"Invalid configuration at {location}: {exc.message}"
        ) from exc


def load_design(path: Path) -> Design:
    path = path.resolve()
    return parse_design(read_document(path), path.parent)


def parse_design(data: object, directory: Path) -> Design:
    validate_document(data, "design.schema.json")
    devices = tuple(
        Device(
            name=item["name"],
            step=(directory / item["step"]).resolve(),
            position=tuple(item["position"]),
            rotation_deg=item["rotation_deg"],
            standoff=item["standoff"],
            mount_hole_diameter=item["mount_hole_diameter"],
            mounts=tuple(tuple(point) for point in item["mounts"]),
            keepouts=tuple(
                Keepout(box["name"], tuple(box["min"]), tuple(box["max"]))
                for box in item.get("keepouts", [])
            ),
        )
        for item in data["devices"]
    )
    names = [device.name for device in devices]
    if len(set(names)) != len(names) or "mounting_plate" in names:
        raise DesignError("Device names must be unique and not 'mounting_plate'")
    for device in devices:
        if device.step.suffix.lower() not in {".step", ".stp"}:
            raise DesignError(f"{device.name}: input must be a STEP file")
        for box in device.keepouts:
            if any(low >= high for low, high in zip(box.minimum, box.maximum)):
                raise DesignError(
                    f"{device.name}/{box.name}: keepout min must be < max"
                )
    return Design(
        Plate(**data["plate"]),
        Mounting(**data["mounting"]),
        data["clearance"],
        devices,
    )
