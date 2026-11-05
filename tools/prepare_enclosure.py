"""Install the custom CSV required by cq_enclosure_builder's documented setup."""

import sys
from importlib.metadata import distribution
from pathlib import Path

if sys.prefix == sys.base_prefix:
    raise SystemExit("Run this command inside the project's virtual environment")

source = Path(
    distribution("cq-enclosure-builder").locate_file(
        "cq_enclosure_builder/screws/flat_head_parameters.csv"
    )
)
target = Path(
    distribution("cq-warehouse").locate_file("cq_warehouse/flat_head_parameters.csv")
)
data = source.read_bytes()
if target.exists():
    if target.read_bytes() != data:
        raise SystemExit(f"Refusing to overwrite different upstream data: {target}")
else:
    with target.open("xb") as stream:
        stream.write(data)
print(f"Upstream enclosure screw data ready: {target}")
