"""Build a parametric, web-ready handpan (hang drum) and export it as a Draco GLB.

Run headless (from the project root):

    "C:/Program Files/Blender Foundation/Blender 5.3/blender.exe" --background --factory-startup \
        --python blender/scripts/handpan/build_handpan.py -- [--no-render] [--samples 96] [--out output/HANDPAN]

Outputs (in --out):
    handpan.glb          Draco-compressed GLB for the web (only the two handpan meshes)
    handpan.blend        the built scene, including the preview studio
    handpan_preview.png, handpan_preview_top.png  Cycles preview renders (skipped with --no-render)
    build_report.json    measured dimensions, triangle counts, file size

Geometry is generated directly as a polar grid (one surface of revolution with height offsets), so
the triangle count is exact and predictable. Colour variation (darker tone-field borders) lives in a
vertex-colour attribute wired straight into Base Color, which survives glTF export as COLOR_0;
no procedural textures are used.

Units: metres. Z up. Origin at the base centre (lowest point of the instrument sits on z = 0).
Front of the instrument (player side, lowest tone field) faces -Y.

These are concept dimensions typical of a 9-note handpan (ding + 8). They are an assumption, not a
measured instrument.
"""

import argparse
import json
import math
import os
import sys

import bpy

# --------------------------------------------------------------------------- parameters
P = {
    # overall form
    "shell_radius": 0.262,      # radius where the shells meet (m)
    "band_radius": 0.265,       # outer radius of the rim band -> 0.53 m overall diameter
    "top_height": 0.114,       # dome rise of the top shell above the seam (m)
    "bottom_height": 0.115,     # dome depth of the bottom shell below the seam (m)
    "dome_exponent": 2.1,       # z = h * (1 - t^k); 2 = paraboloid
    # ding (central raised note)
    "ding_radius": 0.062,
    "ding_height": 0.018,
    "ding_field_width": 0.026,  # shallow field around the ding
    # Gu port in the bottom shell
    "gu_radius": 0.062,         # 12.4 cm hole
    "gu_lip_depth": 0.020,      # the lip turns inward (up into the body)
    # tone fields (8): semi-axis along the radius (a) in metres, in playing order
    "field_ring_radius": 0.170,
    "field_sizes": [0.058, 0.056, 0.054, 0.052, 0.050, 0.048, 0.046, 0.044],
    "field_aspect": 0.78,       # tangential semi-axis b = aspect * a
    "field_depth": 0.0030,      # flattened/depressed field
    "field_groove": 0.0010,     # hammered border groove
    "dimple_ratio": 0.42,       # dimple radius as a fraction of the field
    "dimple_depth": 0.0065,     # dimples are concave on the top shell
    # tessellation (sets the triangle budget)
    "segments": 144,            # around the axis
    "top_rings": 56,            # centre -> seam
    "bottom_rings": 22,         # seam -> Gu
    "band_profile": [           # C-shaped rim band profile: (radial offset from band_radius, z offset from seam)
        (-0.0030, 0.0120), (-0.0008, 0.0108), (0.0, 0.0060), (0.0, 0.0),
        (0.0, -0.0060), (-0.0008, -0.0108), (-0.0030, -0.0120)],
    # colours (linear RGB). Nitrided steel: dark bronze-brown.
    "steel_color": (0.235, 0.140, 0.068),
    "field_rim_dark": 0.50,     # border colour multiplier
    "dimple_tint": 1.10,        # slightly brighter polished dimples
    "steel_metallic": 0.88,
    "steel_roughness": 0.40,
    "band_color": (0.040, 0.034, 0.030),
    "band_metallic": 0.75,
    "band_roughness": 0.48,
    "sharp_angle_deg": 48.0,
}


# --------------------------------------------------------------------------- helpers
def smoothstep(e0, e1, x):
    t = min(1.0, max(0.0, (x - e0) / (e1 - e0)))
    return t * t * (3.0 - 2.0 * t)


def gauss(x, w):
    return math.exp(-(x / w) ** 2)


def dome(t, k):
    return 1.0 - min(1.0, t) ** k


def tone_fields(p):
    """Return the tone-field ellipses: centre angle, ring radius, a, b. Zig-zag layout around the ding."""
    n = len(p["field_sizes"])
    step = 2.0 * math.pi / n
    fields = []
    for i, a in enumerate(p["field_sizes"]):
        # playing order zig-zags left/right, starting at the front (-Y)
        k = (i + 1) // 2
        side = 1 if i % 2 else -1
        slot = (side * k) % n
        theta = -math.pi / 2 + step * slot  # slot 0 sits at the front (-Y)
        rho = p["field_ring_radius"] + (a - p["field_sizes"][0]) * 0.35
        fields.append({"theta": theta, "rho": rho, "a": a, "b": a * p["field_aspect"], "note": i + 1})
    return fields


def top_offset_and_shade(x, y, p, fields):
    """Height offset (m) above the plain dome and a colour multiplier for a point on the top shell."""
    r = math.hypot(x, y)
    dz = 0.0
    shade = 1.0
    # ding: C1-smooth bump
    rd = p["ding_radius"]
    if r < rd:
        s = r / rd
        dz += p["ding_height"] * (1.0 - s * s) ** 2
    ring = r - (rd + p["ding_field_width"] * 0.45)
    dz -= 0.0022 * gauss(ring, p["ding_field_width"] * 0.45)
    shade *= 1.0 - (1.0 - p["field_rim_dark"]) * gauss(r - (rd + p["ding_field_width"]), 0.011)
    # tone fields
    for f in fields:
        cx, cy = f["rho"] * math.cos(f["theta"]), f["rho"] * math.sin(f["theta"])
        dx, dy = x - cx, y - cy
        ux, uy = math.cos(f["theta"]), math.sin(f["theta"])
        u = (dx * ux + dy * uy) / f["a"]
        v = (-dx * uy + dy * ux) / f["b"]
        q = math.hypot(u, v)
        if q > 1.6:
            continue
        dz -= p["field_depth"] * (1.0 - smoothstep(0.72, 1.05, q))
        dz -= p["field_groove"] * gauss(q - 1.02, 0.13)
        qd = q / p["dimple_ratio"]
        if qd < 1.0:
            dz -= p["dimple_depth"] * 0.5 * (1.0 + math.cos(math.pi * qd))
            shade *= 1.0 + (p["dimple_tint"] - 1.0) * (1.0 - qd)
        shade *= 1.0 - (1.0 - p["field_rim_dark"]) * gauss(q - 1.0, 0.24)
    return dz, shade


# --------------------------------------------------------------------------- geometry
def build_body(p, fields):
    R, n = p["shell_radius"], p["segments"]
    k = p["dome_exponent"]
    zb = p["bottom_height"]  # seam height before the final shift; bottom centre would sit at 0
    verts, shades, faces = [], [], []
    rings = []  # each ring is a list of vertex indices (len n), the pole is separate

    def add_ring(points_xyz_shade):
        idx = []
        for (x, y, z, s) in points_xyz_shade:
            idx.append(len(verts))
            verts.append((x, y, z))
            shades.append(s)
        rings.append(idx)

    angles = [2.0 * math.pi * j / n for j in range(n)]

    # top pole
    dz0, s0 = top_offset_and_shade(0.0, 0.0, p, fields)
    pole = len(verts)
    verts.append((0.0, 0.0, zb + p["top_height"] + dz0))
    shades.append(s0)

    # top shell rings (centre -> seam)
    for i in range(1, p["top_rings"] + 1):
        r = R * i / p["top_rings"]
        pts = []
        for a in angles:
            x, y = r * math.cos(a), r * math.sin(a)
            dz, s = top_offset_and_shade(x, y, p, fields)
            fade = 1.0 - smoothstep(0.90, 1.0, r / R)  # keep the seam exactly on the dome
            pts.append((x, y, zb + p["top_height"] * dome(r / R, k) + dz * fade, 1.0 + (s - 1.0) * fade))
        add_ring(pts)
    seam_ring = len(rings) - 1

    # bottom shell rings (seam -> Gu), excluding the seam ring itself
    rg = p["gu_radius"]
    r_end = rg + 0.008
    nb = p["bottom_rings"]
    for i in range(1, nb + 1):
        r = R - (R - r_end) * i / nb
        z = zb - p["bottom_height"] * dome(r / R, k)
        add_ring([(r * math.cos(a), r * math.sin(a), z, 1.0) for a in angles])
    z_gu = zb - p["bottom_height"] * dome(r_end / R, k)

    # Gu lip rolling inward (up into the body); slightly darker like a worked edge
    lip = [(rg + 0.0040, 0.0030, 0.85), (rg + 0.0012, 0.0080, 0.75),
           (rg + 0.0002, 0.0140, 0.75), (rg, p["gu_lip_depth"], 0.75)]
    for (r, dzl, s) in lip:
        add_ring([(r * math.cos(a), r * math.sin(a), z_gu + dzl, s) for a in angles])

    # faces: pole fan
    first = rings[0]
    for j in range(n):
        faces.append((pole, first[j], first[(j + 1) % n]))
    # quads between consecutive rings (outward normals: top rings go outward, bottom inward -> same winding works)
    for ri in range(len(rings) - 1):
        a_r, b_r = rings[ri], rings[ri + 1]
        for j in range(n):
            j2 = (j + 1) % n
            faces.append((a_r[j], b_r[j], b_r[j2], a_r[j2]))

    me = bpy.data.meshes.new("Handpan_Body")
    me.from_pydata(verts, [], faces)
    me.validate()
    me.update()
    col = me.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
    base = p["steel_color"]
    for vi, s in enumerate(shades):
        col.data[vi].color = (base[0] * s, base[1] * s, base[2] * s, 1.0)
    me.color_attributes.active_color = col
    me.color_attributes.render_color_index = 0
    return me, seam_ring


def shell_radius_at_seam_offset(p, dz, top):
    """Radius of the plain dome at height dz above (top) / below the seam."""
    h = p["top_height"] if top else p["bottom_height"]
    t = (1.0 - abs(dz) / h) ** (1.0 / p["dome_exponent"])
    return p["shell_radius"] * t


def build_band(p, zb):
    n = p["segments"]
    prof = []
    # tuck both ends 2 mm inside the shell so there is no visible gap
    z_top_end = p["band_profile"][0][1]
    prof.append((shell_radius_at_seam_offset(p, z_top_end, True) - 0.002, z_top_end))
    for (dr, dz) in p["band_profile"]:
        prof.append((p["band_radius"] + dr, dz))
    z_bot_end = p["band_profile"][-1][1]
    prof.append((shell_radius_at_seam_offset(p, z_bot_end, False) - 0.002, z_bot_end))

    verts, faces = [], []
    for (r, dz) in prof:
        for j in range(n):
            a = 2.0 * math.pi * j / n
            verts.append((r * math.cos(a), r * math.sin(a), zb + dz))
    for i in range(len(prof) - 1):
        for j in range(n):
            j2 = (j + 1) % n
            a0, a1 = i * n + j, i * n + j2
            b0, b1 = (i + 1) * n + j, (i + 1) * n + j2
            faces.append((a0, b0, b1, a1))
    me = bpy.data.meshes.new("Handpan_RimBand")
    me.from_pydata(verts, [], faces)
    me.validate()
    me.update()
    return me


# --------------------------------------------------------------------------- materials
def principled(mat):
    return next(nd for nd in mat.node_tree.nodes if nd.type == "BSDF_PRINCIPLED")


def make_steel_material(p):
    mat = bpy.data.materials.new("MAT_Handpan_NitridedSteel")
    mat.use_nodes = True
    bsdf = principled(mat)
    attr = mat.node_tree.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = "Col"
    attr.location = (-400, 200)
    mat.node_tree.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Metallic"].default_value = p["steel_metallic"]
    bsdf.inputs["Roughness"].default_value = p["steel_roughness"]
    mat.use_backface_culling = False  # -> glTF doubleSided: the inside is visible through the Gu
    mat.diffuse_color = (*p["steel_color"], 1.0)
    return mat


def make_band_material(p):
    mat = bpy.data.materials.new("MAT_Handpan_RimBand")
    mat.use_nodes = True
    bsdf = principled(mat)
    bsdf.inputs["Base Color"].default_value = (*p["band_color"], 1.0)
    bsdf.inputs["Metallic"].default_value = p["band_metallic"]
    bsdf.inputs["Roughness"].default_value = p["band_roughness"]
    mat.use_backface_culling = True
    mat.diffuse_color = (*p["band_color"], 1.0)
    return mat


# --------------------------------------------------------------------------- scene
def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = "METRIC"
    sc.unit_settings.scale_length = 1.0


def finish_object(name, me, mat, coll, sharp_deg):
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    me.shade_smooth()
    me.set_sharp_from_angle(angle=math.radians(sharp_deg))
    return ob


def tri_count(ob):
    me = ob.data
    me.calc_loop_triangles()
    return len(me.loop_triangles)


def build_studio(sc, size):
    """Preview-only product studio (not exported): dark cyclorama, softbox key, strip lights for
    long specular streaks on the metal, warm back rim, and Blender's bundled studio HDRI for
    reflections only (camera rays see a plain dark backdrop)."""
    coll = bpy.data.collections.new("PreviewStudio")
    sc.collection.children.link(coll)

    # curved cyclorama: floor sweeping up into a back wall, so there is no horizon line
    prof = [(-4.0, 0.0), (0.5, 0.0)]
    rad = 1.4
    for i in range(1, 9):
        a = (math.pi / 2) * i / 8
        prof.append((0.5 + rad * math.sin(a), rad * (1 - math.cos(a))))
    prof.append((0.5 + rad, 4.0))
    xs = (-5.0, 5.0)
    verts = [(x, y, z) for (y, z) in prof for x in xs]
    faces = [(2 * i, 2 * i + 1, 2 * i + 3, 2 * i + 2) for i in range(len(prof) - 1)]
    me = bpy.data.meshes.new("PREVIEW_Cyclorama")
    me.from_pydata(verts, [], faces)
    me.shade_smooth()
    floor = bpy.data.objects.new("PREVIEW_Cyclorama", me)
    fm = bpy.data.materials.new("PREVIEW_Cyclorama")
    fm.use_nodes = True
    fb = principled(fm)
    fb.inputs["Base Color"].default_value = (0.018, 0.016, 0.014, 1.0)
    fb.inputs["Roughness"].default_value = 0.9
    fb.inputs["Specular IOR Level"].default_value = 0.08  # near-matte: no glossy spill from the softboxes
    me.materials.append(fm)

    target = bpy.data.objects.new("PREVIEW_Target", None)
    target.location = (0, 0, size * 0.36)

    def area(name, loc, energy, sx, sy, color=(1, 1, 1), diffuse=True, spread_deg=180.0):
        ld = bpy.data.lights.new(name, "AREA")
        ld.shape = "RECTANGLE"
        ld.energy, ld.size, ld.size_y, ld.color = energy, sx, sy, color
        lo = bpy.data.objects.new(name, ld)
        lo.location = loc
        d = target.location - lo.location
        lo.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
        lo.visible_camera = False
        # reflector-only lights shape the metal without washing out the dark backdrop
        lo.visible_diffuse = diffuse
        ld.spread = math.radians(spread_deg)
        return lo
    lights = [
        area("PREVIEW_Key_Softbox", (0.95, -0.85, 1.45), 30, 1.2, 0.8, (1.0, 0.93, 0.84), spread_deg=70.0),
        area("PREVIEW_Top_Overhead", (0.0, 0.1, 2.1), 16, 2.0, 1.2, (1.0, 0.97, 0.92), diffuse=False),
        area("PREVIEW_Strip_Left", (-1.15, -0.25, 0.65), 45, 0.12, 1.6, (0.88, 0.93, 1.0), diffuse=False),
        area("PREVIEW_Strip_Right", (1.25, 0.35, 0.55), 30, 0.12, 1.6, (1.0, 0.95, 0.9), diffuse=False),
        area("PREVIEW_Rim_Back", (-0.2, 1.25, 0.75), 70, 1.4, 0.25, (1.0, 0.82, 0.6), spread_deg=60.0),
    ]

    cams = []
    for name, loc, lens in (("PREVIEW_Camera", (0.30, -1.38, 0.74), 78),
                            ("PREVIEW_Camera_Top", (0.0, -0.62, 1.42), 60)):
        cd = bpy.data.cameras.new(name)
        cd.lens = lens
        cd.dof.use_dof = name == "PREVIEW_Camera"  # a hint of depth on the hero view only
        cd.dof.focus_object = target
        cd.dof.aperture_fstop = 16.0
        cam = bpy.data.objects.new(name, cd)
        cam.location = loc
        tc = cam.constraints.new("TRACK_TO")
        tc.target, tc.track_axis, tc.up_axis = target, "TRACK_NEGATIVE_Z", "UP_Y"
        cams.append(cam)

    for o in [floor, *lights, *cams, target]:
        coll.objects.link(o)
    sc.camera = cams[0]

    # world: HDRI lights reflections at low strength, camera rays see a dark backdrop
    world = bpy.data.worlds.new("PREVIEW_World")
    world.use_nodes = True
    nt = world.node_tree
    bg = next(nd for nd in nt.nodes if nd.type == "BACKGROUND")
    out_node = next(nd for nd in nt.nodes if nd.type == "OUTPUT_WORLD")
    hdri_dir = bpy.utils.system_resource("DATAFILES", path="studiolights/world")
    hdri = os.path.join(hdri_dir, "studio.exr") if hdri_dir else ""
    if hdri and os.path.exists(hdri):
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(hdri, check_existing=True)
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = 0.06
        dark = nt.nodes.new("ShaderNodeBackground")
        dark.inputs["Color"].default_value = (0.012, 0.011, 0.010, 1.0)
        lp = nt.nodes.new("ShaderNodeLightPath")
        mix = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs[0])
        nt.links.new(bg.outputs[0], mix.inputs[1])
        nt.links.new(dark.outputs[0], mix.inputs[2])
        nt.links.new(mix.outputs[0], out_node.inputs["Surface"])
    else:
        print("studio HDRI not found; using a flat world")
        bg.inputs["Color"].default_value = (0.03, 0.028, 0.026, 1.0)
    sc.world = world
    return cams


def setup_cycles(sc, samples):
    try:
        sc.render.engine = "CYCLES"
    except TypeError as e:
        print("Cycles not available:", e)
        return
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices()
        for d in prefs.devices:
            d.use = True
        sc.cycles.device = "GPU"
    except Exception as e:  # fall back to CPU
        print("GPU setup failed, rendering on CPU:", e)
    sc.render.resolution_x, sc.render.resolution_y = 1400, 1000
    sc.render.image_settings.file_format = "PNG"
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Punchy"
        sc.view_settings.exposure = -1.3  # keep the bronze highlights from clipping to cream
    except TypeError as e:
        print("colour management:", e)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="output/HANDPAN")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--samples", type=int, default=96)
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    clear_scene()
    sc = bpy.context.scene
    coll = bpy.data.collections.new("Handpan")
    sc.collection.children.link(coll)

    fields = tone_fields(P)
    body_me, _ = build_body(P, fields)
    zb = P["bottom_height"]
    band_me = build_band(P, zb)

    # shift so the lowest point sits on z = 0 (origin = base centre)
    zmin = min(v.co.z for v in body_me.vertices)
    for me in (body_me, band_me):
        for v in me.vertices:
            v.co.z -= zmin
        me.update()

    body = finish_object("Handpan_Body", body_me, make_steel_material(P), coll, P["sharp_angle_deg"])
    band = finish_object("Handpan_RimBand", band_me, make_band_material(P), coll, 60.0)

    xs = [v.co.x for v in body_me.vertices] + [v.co.x for v in band_me.vertices]
    zs = [v.co.z for v in body_me.vertices] + [v.co.z for v in band_me.vertices]
    dims = {"diameter_m": round(max(xs) - min(xs), 4), "height_m": round(max(zs) - min(zs), 4),
            "seam_height_m": round(zb - zmin, 4), "gu_diameter_m": 2 * P["gu_radius"]}
    tris = {"Handpan_Body": tri_count(body), "Handpan_RimBand": tri_count(band)}
    tris["total"] = sum(tris.values())
    print("DIMENSIONS", dims)
    print("TRIANGLES", tris)

    # export only the handpan meshes
    bpy.ops.object.select_all(action="DESELECT")
    for o in (body, band):
        o.select_set(True)
    bpy.context.view_layer.objects.active = body
    glb = os.path.join(out, "handpan.glb")
    bpy.ops.export_scene.gltf(
        filepath=glb, export_format="GLB", use_selection=True, export_apply=True,
        export_yup=True, export_normals=True, export_texcoords=False, export_materials="EXPORT",
        export_draco_mesh_compression_enable=True, export_draco_mesh_compression_level=6,
        export_draco_position_quantization=14, export_draco_normal_quantization=10,
        export_draco_color_quantization=10, export_cameras=False, export_lights=False,
        export_extras=False)
    size_bytes = os.path.getsize(glb)
    print("GLB", glb, size_bytes, "bytes")

    cams = build_studio(sc, dims["height_m"])
    preview = None
    if not args.no_render:
        setup_cycles(sc, args.samples)
        preview = []
        for cam, fname in zip(cams, ("handpan_preview.png", "handpan_preview_top.png")):
            sc.camera = cam
            path = os.path.join(out, fname)
            sc.render.filepath = path
            bpy.ops.render.render(write_still=True)
            preview.append(os.path.relpath(path))
            print("PREVIEW", path)
        sc.camera = cams[0]

    blend = os.path.join(out, "handpan.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend, compress=True)

    report = {"blender": bpy.app.version_string, "dimensions": dims, "triangles": tris,
              "tone_fields": [{"note": f["note"], "angle_deg": round(math.degrees(f["theta"]) % 360, 1),
                               "semi_axes_m": [round(f["a"], 4), round(f["b"], 4)]} for f in fields],
              "glb": os.path.relpath(glb), "glb_bytes": size_bytes, "preview": preview,
              "blend": os.path.relpath(blend), "params": P}
    with open(os.path.join(out, "build_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print("DONE")


if __name__ == "__main__":
    main()
