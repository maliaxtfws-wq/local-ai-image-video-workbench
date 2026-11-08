"""Opt-in R2V identity reference plus the existing 22-frame continuation guide."""
import hashlib
import threading
from pathlib import Path

MODEL_NAME = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
MODEL_SIZE = 20970379616
MODEL_SHA256 = "9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779"
MODEL_URL = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/" + MODEL_NAME
_verified = set()
_lock = threading.Lock()


def verify_model(comfy_root):
    path = Path(comfy_root) / "models" / "diffusion_models" / MODEL_NAME
    with _lock:
        if not path.is_file() or path.stat().st_size != MODEL_SIZE:
            raise ValueError("双参考实验的 R2V 模型尚未安装完整；原续接方案仍可使用。")
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if key not in _verified:
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != MODEL_SHA256 or path.stat().st_mtime_ns != stat.st_mtime_ns:
                raise ValueError("R2V 模型校验不一致，未提交生成。")
            _verified.add(key)
    return path


def apply_dual_reference(graph, image_name):
    if not image_name:
        raise ValueError("双参考续接需要本镜头表保存的原始角色首图。")
    graph["1"]["inputs"]["unet_name"] = MODEL_NAME
    inputs = graph["5"]["inputs"]
    graph["5"]["class_type"] = "MiniMaxH3ReferenceToVideo"
    # V3 Autogrow requires its parent name in API keys. Bare ref_image_0
    # bypasses nesting and is rejected by ReferenceToVideo.execute().
    inputs.update({"audio_vae":["4",0], "ref_image_size":"match",
                   "ref_images.ref_image_0":["22",0], "ref_videos.ref_video_0":["19",0],
                   "ref_video_audios.ref_video_audio_0":["19",1]})
    inputs["prompt"] = (
        "<Picture 1> is the original character identity reference. Preserve this person's face, hair and clothing. "
        "<Video 1> provides the preceding action, scene and camera continuity; use <Audio 1> for ambient sound continuity. "
        "If identity differs, follow <Picture 1>, not facial drift in <Video 1>. Continue the action naturally; "
        "do not restart the original picture pose, show a reference collage, or cut to another scene.\n"
        + inputs["prompt"]
    )
    graph["22"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
    # Keep node 20: the tail is still anchored at frame zero, retaining merge's 22-frame contract.
    return graph
