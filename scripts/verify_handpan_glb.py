"""Re-open the exported handpan GLB in an empty Blender scene and check it.

    "C:/Program Files/Blender Foundation/Blender 5.3/blender.exe" --background --factory-startup \
        --python blender/scripts/handpan/verify_handpan_glb.py -- [--glb output/HANDPAN/handpan.glb]

Checks: scale (bounding box in metres), base on z = 0, triangle count, materials and their PBR values,
vertex colours, Draco compression and doubleSided flags (read from the GLB JSON chunk).
Writes verify_report.json next to the GLB and exits with code 1 if a check fails.
"""

import argparse
import json
import os
import struct
import sys

import bpy
from mathutils import Vector

LIMITS = {"diameter": (0.52, 0.54), "height": (0.23, 0.25), "max_tris": 30000, "max_bytes": 2_000_000}


def glb_json(path):
    with open(path, "rb") as fh:
        magic, _ver, _len = struct.unpack("<4sII", fh.read(12))
        assert magic == b"glTF", "not a GLB"
        clen, ctype = struct.unpack("<I4s", fh.read(8))
        assert ctype == b"JSON"
        return json.loads(fh.read(clen))


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--glb", default="output/HANDPAN/handpan.glb")
    glb = os.path.abspath(ap.parse_args(argv).glb)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]

    lo, hi = Vector((1e9,) * 3), Vector((-1e9,) * 3)
    tris, objs = 0, []
    for o in meshes:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
        o.data.calc_loop_triangles()
        n = len(o.data.loop_triangles)
        tris += n
        mats = []
        for m in o.data.materials:
            b = next(nd for nd in m.node_tree.nodes if nd.type == "BSDF_PRINCIPLED")
            bc = b.inputs["Base Color"]
            mats.append({"name": m.name, "metallic": round(b.inputs["Metallic"].default_value, 3),
                         "roughness": round(b.inputs["Roughness"].default_value, 3),
                         "base_color": "linked:" + bc.links[0].from_node.type if bc.is_linked
                         else [round(v, 3) for v in bc.default_value[:3]]})
        objs.append({"name": o.name, "triangles": n, "color_attributes": [a.name for a in o.data.color_attributes],
                     "materials": mats, "location": list(o.location), "scale": list(o.scale)})

    j = glb_json(glb)
    size = os.path.getsize(glb)
    dims = {"x": round(hi.x - lo.x, 4), "y": round(hi.y - lo.y, 4), "z": round(hi.z - lo.z, 4), "min_z": round(lo.z, 5)}
    checks = {
        "diameter_ok": LIMITS["diameter"][0] <= max(dims["x"], dims["y"]) <= LIMITS["diameter"][1],
        "height_ok": LIMITS["height"][0] <= dims["z"] <= LIMITS["height"][1],
        "base_on_ground": abs(dims["min_z"]) < 1e-3,
        "tris_ok": tris <= LIMITS["max_tris"],
        "size_ok": size <= LIMITS["max_bytes"],
        "draco": "KHR_draco_mesh_compression" in j.get("extensionsRequired", []),
        "vertex_colors": any("COLOR_0" in pr["attributes"] for m in j["meshes"] for pr in m["primitives"]),
        "materials": sorted(m["name"] for m in j["materials"]) == ["MAT_Handpan_NitridedSteel", "MAT_Handpan_RimBand"],
    }
    report = {"glb": glb, "bytes": size, "dimensions_m": dims, "triangles": tris, "objects": objs,
              "gltf_materials": j["materials"], "checks": checks, "all_ok": all(checks.values())}
    with open(os.path.join(os.path.dirname(glb), "verify_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["all_ok"] else 1)


if __name__ == "__main__":
    main()
