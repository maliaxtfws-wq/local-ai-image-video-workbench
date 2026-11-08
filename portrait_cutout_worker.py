"""CPU-only BiRefNet portrait cutout worker.

This worker runs under the dedicated Python 3.12 image runtime so the H3
Python environment does not need ONNX Runtime.  It writes RGBA PNGs whose RGB
pixels are already composited over a neutral background; ComfyUI's LoadImage
currently drops alpha on its IMAGE output, so this keeps the isolated-person
preprocessing effective while retaining an audit-friendly alpha channel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps


MODEL_SIZE = 972_666_916
MODEL_SHA256 = "58f621f00f5d756097615970a88a791584600dcf7c45b18a0a6267535a1ebd3c"
INPUT_SIZE = 1024
MAX_IMAGE_PIXELS = 40_000_000
NEUTRAL_RGB = np.asarray([235, 235, 235], dtype=np.float32)
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        if opened.width * opened.height > MAX_IMAGE_PIXELS:
            raise ValueError(f"图片像素超过 4000 万：{path.name}")
        opened.verify()
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def input_tensor(image: Image.Image) -> np.ndarray:
    resized = image.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
    value = np.asarray(resized, dtype=np.float32) / 255.0
    value = (value - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)) / np.asarray(
        [0.229, 0.224, 0.225], dtype=np.float32
    )
    return np.transpose(value, (2, 0, 1))[None]


def render_cutout(session: ort.InferenceSession, source: Path, output: Path) -> dict[str, object]:
    started = time.monotonic()
    image = load_rgb(source)
    model_input = session.get_inputs()[0].name
    logits = session.run(None, {model_input: input_tensor(image)})[0][0, 0]
    alpha = 1.0 / (1.0 + np.exp(-np.clip(logits, -50.0, 50.0)))
    alpha_min = float(alpha.min())
    alpha_range = float(alpha.max()) - alpha_min
    if alpha_range < 1e-6:
        raise ValueError(f"参考图人物蒙版没有有效层次：{source.name}")
    alpha = (alpha - alpha_min) / alpha_range
    alpha_image = Image.fromarray(np.rint(alpha * 255.0).astype(np.uint8), mode="L")
    alpha_image = alpha_image.resize(image.size, Image.Resampling.LANCZOS)
    alpha_u8 = np.asarray(alpha_image, dtype=np.uint8)
    alpha_float = alpha_u8.astype(np.float32)[..., None] / 255.0
    original = np.asarray(image, dtype=np.float32)
    isolated = np.rint(original * alpha_float + NEUTRAL_RGB * (1.0 - alpha_float)).astype(np.uint8)
    rgba = np.dstack((isolated, alpha_u8))

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.part")
    try:
        Image.fromarray(rgba, mode="RGBA").save(temporary, format="PNG", optimize=True)
        with Image.open(temporary) as check:
            check.verify()
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise

    foreground = alpha_u8 >= 128
    ys, xs = np.nonzero(foreground)
    box = None if not len(xs) else [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    coverage = float(alpha_u8.mean() / 255.0)
    warnings: list[str] = []
    if box is None or coverage < 0.005:
        raise ValueError(f"没有在参考图中识别到可用人物：{source.name}")
    if coverage > 0.98:
        warnings.append("前景覆盖率很高；这张图可能已经没有明显背景。")
    return {
        "source": str(source),
        "output": str(output),
        "width": image.width,
        "height": image.height,
        "foreground_coverage": round(coverage, 6),
        "foreground_box": box,
        "output_sha256": sha256_file(output),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", action="append", required=True)
    arguments = parser.parse_args()

    model = Path(arguments.model).resolve()
    inputs = [Path(value).resolve() for value in arguments.input]
    outputs = [Path(value).resolve() for value in arguments.output]
    if len(inputs) != len(outputs):
        raise ValueError("--input 与 --output 数量必须一致。")
    if not model.is_file() or model.stat().st_size != MODEL_SIZE:
        raise ValueError("BiRefNet-General ONNX 模型缺失或大小不匹配。")
    model_sha256 = sha256_file(model)
    if model_sha256 != MODEL_SHA256:
        raise ValueError("BiRefNet-General ONNX 模型哈希与已登记版本不一致。")
    for source in inputs:
        if not source.is_file():
            raise ValueError(f"找不到参考图：{source}")

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 4) // 2))
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    load_started = time.monotonic()
    session = ort.InferenceSession(
        str(model), sess_options=options, providers=["CPUExecutionProvider"]
    )
    model_load_seconds = round(time.monotonic() - load_started, 3)
    results: list[dict[str, object]] = []
    try:
        for source, output in zip(inputs, outputs, strict=True):
            results.append(render_cutout(session, source, output))
    except Exception:
        for output in outputs:
            output.unlink(missing_ok=True)
        raise

    print(
        json.dumps(
            {
                "model": "BiRefNet-General ONNX",
                "model_path": str(model),
                "model_sha256": model_sha256,
                "provider": session.get_providers()[0],
                "model_load_seconds": model_load_seconds,
                "results": results,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
