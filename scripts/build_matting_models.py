"""Build app/matting_patch.bin, which wraps the Robust Video Matting models for the GPU pipeline.

The patch adds nodes that take the camera frame as uint8 RGBA (H, W, 4) and return the
composited frame as uint8 RGB (H, W, 3): they normalise the frame, feed RVM, build a blurred
background that excludes the person, and composite RVM's colour-decontaminated foreground.
The app splices the patch into the official model at runtime (see matting.splice).

Inputs:  frame uint8 (H, W, 4); background uint8 (H, W, 3) or (1, 1, 3); blur_mix float (1,);
         blur_kernel float (8, 1, 1, KERNEL_TAPS); r1i..r4i recurrent state; downsample_ratio float (1,)
Outputs: output uint8 (H, W, 3); r1o..r4o recurrent state

Usage: py -3 scripts\\build_matting_models.py   (requires: pip install onnx; downloads to build\\models)
"""
from __future__ import annotations

from pathlib import Path
import struct
import sys

import numpy as np
import onnx
from onnx import GraphProto, TensorProto, helper, numpy_helper

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "app"))
import matting  # noqa: E402

KERNEL_TAPS = matting.KERNEL_TAPS
BLUR_POOL = 4  # The background is blurred at quarter resolution.


def build_patch(model: onnx.ModelProto) -> bytes:
    graph = model.graph
    source = next(value for value in graph.input if value.name == "src")
    model_type = source.type.tensor_type.elem_type
    pre, post, initializers = [], [], []

    def const(name, value):
        initializers.append(numpy_helper.from_array(np.asarray(value), name))
        return name

    def node(target, op, inputs, output, **attributes):
        target.append(helper.make_node(op, inputs, [output], name=output, **attributes))
        return output

    # Preprocess: uint8 HWC RGBA -> model-precision NCHW RGB in 0..1.
    rgb = node(pre, "Slice", ["frame", const("c0", np.array([0], np.int64)), const("c3", np.array([3], np.int64)),
                               const("axis2", np.array([2], np.int64))], "rgb_u8")
    rgb32 = node(pre, "Cast", [rgb], "rgb_f32", to=TensorProto.FLOAT)
    scaled = node(pre, "Mul", [rgb32, const("inv255", np.array(1 / 255, np.float32))], "rgb_scaled")
    chw = node(pre, "Transpose", [scaled], "rgb_chw", perm=[2, 0, 1])
    src32 = node(pre, "Unsqueeze", [chw], "src_f32", axes=[0])
    node(pre, "Cast", [src32], "src", to=model_type)

    # Postprocess in float32.
    fgr = node(post, "Cast", ["fgr"], "fgr_f32", to=TensorProto.FLOAT)
    pha = node(post, "Cast", ["pha"], "pha_f32", to=TensorProto.FLOAT)
    one = const("one", np.array(1, np.float32))
    inverse = node(post, "Sub", [one, pha], "inverse_alpha")

    # Blurred background: blur [src * (1 - alpha), (1 - alpha), src, 1] so the person doesn't smear into it.
    ones = node(post, "Mul", [node(post, "Slice", [src32, const("k0", np.array([0], np.int64)),
                                                   const("k1", np.array([1], np.int64)),
                                                   const("axis1", np.array([1], np.int64))], "src_r"),
                              const("zero", np.array(0, np.float32))], "zeros")
    ones = node(post, "Add", [ones, one], "ones")
    stack = node(post, "Concat", [node(post, "Mul", [src32, inverse], "weighted"), inverse, src32, ones],
                 "blur_stack", axis=1)
    pooled = node(post, "AveragePool", [stack], "pooled", kernel_shape=[BLUR_POOL, BLUR_POOL],
                  strides=[BLUR_POOL, BLUR_POOL], ceil_mode=1)
    radius = KERNEL_TAPS // 2
    horizontal = node(post, "Conv", [pooled, "blur_kernel"], "blur_h", group=8, pads=[0, radius, 0, radius])
    vertical_kernel = node(post, "Transpose", ["blur_kernel"], "blur_kernel_v", perm=[0, 1, 3, 2])
    blurred = node(post, "Conv", [horizontal, vertical_kernel], "blur_hv", group=8, pads=[radius, 0, radius, 0])

    def channels(start, end, name):
        return node(post, "Slice", [blurred, const(f"{name}_s", np.array([start], np.int64)),
                                    const(f"{name}_e", np.array([end], np.int64)), "axis1"], name)

    weighted_sum, weight, plain_sum, count = (channels(0, 3, "b_weighted"), channels(3, 4, "b_weight"),
                                              channels(4, 7, "b_plain"), channels(7, 8, "b_count"))
    epsilon = const("epsilon", np.array(1e-4, np.float32))
    excluded = node(post, "Div", [weighted_sum, node(post, "Max", [weight, epsilon], "b_weight_safe")], "b_excluded")
    plain = node(post, "Div", [plain_sum, node(post, "Max", [count, epsilon], "b_count_safe")], "b_plain_mean")
    # Where the person covers the whole blur window, fall back to an ordinary blur.
    coverage = node(post, "Div", [weight, node(post, "Max", [count, epsilon], "b_count_safe2")], "b_coverage")
    usable = node(post, "Greater", [coverage, const("min_coverage", np.array(0.02, np.float32))], "b_usable")
    small_background = node(post, "Where", [usable, excluded, plain], "b_small")
    full_size = node(post, "Shape", [src32], "src_shape")
    empty = const("empty", np.array([], np.float32))
    blur_background = node(post, "Resize", [small_background, empty, empty, full_size], "b_full",
                           mode="linear", coordinate_transformation_mode="half_pixel")

    given = node(post, "Unsqueeze", [node(post, "Transpose", [
        node(post, "Mul", [node(post, "Cast", ["background"], "bg_f32", to=TensorProto.FLOAT), "inv255"], "bg_scaled")],
        "bg_chw", perm=[2, 0, 1])], "bg_nchw", axes=[0])
    background = node(post, "Add", [given, node(post, "Mul", [
        node(post, "Sub", [blur_background, given], "bg_delta"), "blur_mix"], "bg_mixed_delta")], "bg_final")

    composite = node(post, "Add", [background, node(post, "Mul", [
        node(post, "Sub", [fgr, background], "fg_delta"), pha], "fg_alpha")], "composite")
    clipped = node(post, "Clip", [composite, const("lo", np.array(0, np.float32)), const("hi", np.array(1, np.float32))], "clipped")
    rounded = node(post, "Add", [node(post, "Mul", [clipped, const("c255", np.array(255, np.float32))], "c255x"),
                                 const("half", np.array(0.5, np.float32))], "rounded")
    hwc = node(post, "Transpose", [node(post, "Squeeze", [rounded], "squeezed", axes=[0])], "hwc", perm=[1, 2, 0])
    node(post, "Cast", [hwc], "output", to=TensorProto.UINT8)

    new_inputs = [
        helper.make_tensor_value_info("frame", TensorProto.UINT8, ["height", "width", 4]),
        helper.make_tensor_value_info("background", TensorProto.UINT8, ["bg_height", "bg_width", 3]),
        helper.make_tensor_value_info("blur_mix", TensorProto.FLOAT, [1]),
        helper.make_tensor_value_info("blur_kernel", TensorProto.FLOAT, [8, 1, 1, KERNEL_TAPS]),
    ]
    pre_graph = GraphProto()
    pre_graph.node.extend(pre)
    post_graph = GraphProto()
    post_graph.node.extend(post)
    post_graph.input.extend(new_inputs)
    post_graph.output.append(helper.make_tensor_value_info("output", TensorProto.UINT8, ["height", "width", 3]))
    post_graph.initializer.extend(initializers)
    pre_bytes = pre_graph.SerializeToString()
    return struct.pack("<I", len(pre_bytes)) + pre_bytes + post_graph.SerializeToString()


def main():
    if len(sys.argv) != 1:
        raise SystemExit(__doc__)
    sources = {quality: matting.download_source(quality, REPOSITORY / "build" / "models", print)
               for quality in matting.SOURCES}
    patches = {quality: build_patch(onnx.load(str(path))) for quality, path in sources.items()}
    if len(set(patches.values())) != 1:
        raise SystemExit("The models need different patches; matting.ensure_model supports only one.")
    patch = next(iter(patches.values()))
    for quality, path in sources.items():
        spliced = onnx.load_from_string(matting.splice(path.read_bytes(), patch))
        onnx.checker.check_model(spliced)
        assert [value.name for value in spliced.graph.output] == ["r1o", "r2o", "r3o", "r4o", "output"], quality
    target = matting.patch_path()
    target.write_bytes(patch)
    print(f"Wrote {target} ({len(patch)} bytes)")


if __name__ == "__main__":
    main()
