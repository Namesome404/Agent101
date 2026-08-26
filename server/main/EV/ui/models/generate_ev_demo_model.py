#!/usr/bin/env python3
"""Generate the small, original offline GLB used by the information panel."""
from __future__ import annotations

import json
import struct
from pathlib import Path


POSITIONS = [
    # front, back, top, bottom, right, left (four independent vertices/face)
    (-.5, -.5, .5), (.5, -.5, .5), (.5, .5, .5), (-.5, .5, .5),
    (.5, -.5, -.5), (-.5, -.5, -.5), (-.5, .5, -.5), (.5, .5, -.5),
    (-.5, .5, .5), (.5, .5, .5), (.5, .5, -.5), (-.5, .5, -.5),
    (-.5, -.5, -.5), (.5, -.5, -.5), (.5, -.5, .5), (-.5, -.5, .5),
    (.5, -.5, .5), (.5, -.5, -.5), (.5, .5, -.5), (.5, .5, .5),
    (-.5, -.5, -.5), (-.5, -.5, .5), (-.5, .5, .5), (-.5, .5, -.5),
]
NORMALS = (
    [(0, 0, 1)] * 4 + [(0, 0, -1)] * 4 + [(0, 1, 0)] * 4
    + [(0, -1, 0)] * 4 + [(1, 0, 0)] * 4 + [(-1, 0, 0)] * 4
)
INDICES = [value + face * 4 for face in range(6) for value in (0, 1, 2, 0, 2, 3)]


def node(mesh: int, name: str, translation, scale):
    return {
        "mesh": mesh,
        "name": name,
        "translation": list(translation),
        "scale": list(scale),
    }


def build() -> bytes:
    positions = b"".join(struct.pack("<3f", *value) for value in POSITIONS)
    normals = b"".join(struct.pack("<3f", *value) for value in NORMALS)
    indices = b"".join(struct.pack("<H", value) for value in INDICES)
    binary = positions + normals + indices
    while len(binary) % 4:
        binary += b"\0"

    colors = [
        (0.16, 0.42, 0.95, 1),
        (0.035, 0.055, 0.09, 1),
        (0.18, 0.92, 0.88, 1),
        (0.82, 0.88, 1.0, 1),
    ]
    nodes = [{"name": "EV Demo Robot", "children": list(range(1, 15))}]
    nodes += [
        node(1, "Base", (0, -1.72, 0), (1.45, .16, .88)),
        node(0, "Body", (0, -.08, 0), (1.08, 1.03, .60)),
        node(3, "Chest", (0, .05, .62), (.66, .48, .045)),
        node(0, "Head", (0, 1.22, 0), (.86, .66, .66)),
        node(1, "Visor", (0, 1.30, .68), (.58, .20, .045)),
        node(2, "Antenna", (.42, 2.02, 0), (.065, .43, .065)),
        node(2, "Antenna light", (.42, 2.47, 0), (.14, .14, .14)),
        node(0, "Left arm", (-1.28, -.05, 0), (.18, .76, .22)),
        node(0, "Right arm", (1.28, -.05, 0), (.18, .76, .22)),
        node(2, "Left hand", (-1.28, -.88, 0), (.27, .20, .27)),
        node(2, "Right hand", (1.28, -.88, 0), (.27, .20, .27)),
        node(1, "Left leg", (-.48, -1.26, 0), (.28, .48, .32)),
        node(1, "Right leg", (.48, -1.26, 0), (.28, .48, .32)),
        node(2, "Core", (0, .05, .68), (.16, .16, .05)),
    ]
    document = {
        "asset": {"version": "2.0", "generator": "EV offline demo generator"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "materials": [{
            "name": name,
            "pbrMetallicRoughness": {
                "baseColorFactor": list(color),
                "metallicFactor": .35 if index < 2 else .08,
                "roughnessFactor": .34,
            },
        } for index, (name, color) in enumerate(zip(
            ("EV Blue", "Graphite", "Signal Cyan", "Frost"), colors
        ))],
        "meshes": [{
            "name": "Cube %d" % index,
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1},
                "indices": 2,
                "material": index,
            }],
        } for index in range(4)],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 24,
             "type": "VEC3", "min": [-.5, -.5, -.5], "max": [.5, .5, .5]},
            {"bufferView": 1, "componentType": 5126, "count": 24, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions), "target": 34962},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(normals), "target": 34962},
            {"buffer": 0, "byteOffset": len(positions) + len(normals),
             "byteLength": len(indices), "target": 34963},
        ],
        "buffers": [{"byteLength": len(binary)}],
    }
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    while len(encoded) % 4:
        encoded += b" "
    json_chunk = struct.pack("<I4s", len(encoded), b"JSON") + encoded
    bin_chunk = struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(json_chunk) + len(bin_chunk)) + json_chunk + bin_chunk


if __name__ == "__main__":
    target = Path(__file__).with_name("ev-demo-robot.glb")
    target.write_bytes(build())
    print(target)
