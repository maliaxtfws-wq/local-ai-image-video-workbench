"""Validated sequence settings and lossless-framing first-frame preparation."""
from PIL import Image, ImageOps

SETTING_KEYS = {"quality", "aspect", "steps", "image_fit", "target_shots", "continuation_strategy"}


def settings(data, manifest, *, legacy=False):
    quality = data.get("quality", "draft")
    aspect = data.get("aspect", "landscape" if legacy else "auto")
    fit = data.get("image_fit", "crop" if legacy else "contain")
    if quality not in manifest["presets"] or aspect not in {"auto", "landscape", "portrait", "square"}:
        raise ValueError("画幅或质量档位无效。")
    preset = manifest["presets"][quality]
    steps = data.get("steps")
    if steps is None:
        steps = preset["steps"]
    target = data.get("target_shots", 0)
    strategy = data.get("continuation_strategy", "previous")
    if strategy not in {"previous", "dual_reference"}:
        raise ValueError("续接方案无效。")
    if type(steps) is not int or steps not in {8, 12, 20, 30, 40}:
        raise ValueError("采样步数请选择 8、12、20、30 或 40。")
    if preset.get("turbo") and steps != 8:
        raise ValueError("Turbo 模型固定使用 8 步；更多步数请选择标准或高清档位。")
    if not preset.get("turbo") and steps == 8:
        raise ValueError("非 Turbo 档位请选择 12、20、30 或 40 步。")
    if fit not in {"contain", "crop"}:
        raise ValueError("首图适配方式无效。")
    if type(target) is not int or not 0 <= target <= 1000:
        raise ValueError("目标镜头数为 0（自动）或 1–1000。")
    if strategy == "dual_reference" and (quality not in {"standard", "native"} or steps < 20):
        raise ValueError("双参考实验请选原版标准/高清档和至少 20 步；暂不混用 Turbo 或 ClipProj。")
    return dict(quality=quality, aspect=aspect, steps=steps, image_fit=fit, target_shots=target,
                continuation_strategy=strategy)


def fit_first_frame(source, size, mode):
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image).convert("RGBA")
        background = Image.new("RGBA", oriented.size, (0, 0, 0, 255))
        background.alpha_composite(oriented)
        rgb = background.convert("RGB")
        if mode == "contain":
            return ImageOps.pad(rgb, size, method=Image.Resampling.LANCZOS, color="black")
        if mode == "crop":
            return ImageOps.fit(rgb, size, method=Image.Resampling.LANCZOS)
        raise ValueError("未知首图适配方式。")
