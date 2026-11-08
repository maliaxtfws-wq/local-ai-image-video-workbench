"""Reviewed Qwen Image 2.1 INT8 graph, isolated from the legacy image runtime."""
from pathlib import Path
from aiohttp import ClientTimeout
from image_backend import (ImageBackend, ImageBackendError, _validated_prompt,
    _validated_seed, _validated_steps, _validated_dimensions, _filename_prefix)

MODEL_CONTRACTS_21 = {
    "diffusion_models/qwen_image_2.1_int8_convrot.safetensors": 7256783064,
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors": 9350798360,
    "vae/qwen_image_2.1_vae_bf16.safetensors": 675509688,
}


def build_qwen21(prompt, references=(), *, seed=None, steps=25, canvas=None, resolution=768):
    prompt = _validated_prompt(prompt)
    if len(references) > 10:
        raise ImageBackendError("INVALID_REFERENCE_COUNT", "Qwen 2.1 最多接收 10 张参考图。")
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "negative_prompt": "", "resolution": resolution}},
        "5": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "cpu", "dtype": "int8"}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["4", 2], "seed": _validated_seed(seed), "steps": _validated_steps(steps), "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["6", 0], "vae": ["3", 0], "tile_size": 512, "overlap": 64, "temporal_size": 64, "temporal_overlap": 8}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": _filename_prefix("Qwen-Image-2.1")}},
    }
    for i, name in enumerate(references, 1):
        node = str(20 + i)
        graph[node] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph["4"]["inputs"][f"images.image_{i}"] = [node, 0]
    if canvas:
        width, height = _validated_dimensions(*canvas)
        graph["9"] = {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}
        graph["6"]["inputs"]["latent_image"] = ["9", 0]
    return graph


class Qwen21Backend(ImageBackend):
    def verify_models(self):
        files = []
        for name, size in MODEL_CONTRACTS_21.items():
            path = self.config.comfy_root / "models" / name
            actual = path.stat().st_size if path.is_file() else 0
            files.append(dict(path=str(path), relative_path=name, expected_size=size, actual_size=actual, ready=actual == size))
        return dict(ready=all(f["ready"] for f in files), files=files, model="Qwen-Image-2.1 INT8")

    async def preflight(self, workflow_id=None):
        await self.ensure_started()
        async with self.session.get(f"{self.base_url}/object_info", timeout=ClientTimeout(total=30)) as response:
            response.raise_for_status()
            objects = await response.json()
        required = {n["class_type"] for n in build_qwen21("preflight", ["test.png"], canvas=(512, 512)).values()}
        missing = required - set(objects)
        if missing:
            raise ImageBackendError("PREFLIGHT_MISSING_NODES", "新版图片引擎缺少节点", sorted(missing))
        return dict(ready=True)

    async def _generate(self, prompt, paths, *, seed, steps, canvas, intent):
        await self.preflight()
        uploads = []
        try:
            for i, path in enumerate(paths):
                upload = await self.upload_image(path)
                upload["reference_slot"] = f"image{i + 1}"
                uploads.append(upload)
            graph = build_qwen21(prompt, [u["input_name"] for u in uploads], seed=seed, steps=steps, canvas=canvas)
            result = await self._submit_graph(graph)
        except BaseException:
            self.cleanup_uploads(uploads)
            raise
        result.update(uploads=uploads, metadata=dict(workflow_id="qwen_image_21_int8", reference_intent=intent,
            supplied_reference_count=len(uploads), supplied_reference_slots=[u["reference_slot"] for u in uploads],
            repeated_master_slots=[], runtime_validation="per_job_decode_and_sha256", cfg=1,
            reference_resolution=768, cache_device="cpu", cache_dtype="int8", graph=graph))
        return result

    async def submit_t2i(self, prompt, *, negative_prompt="", seed=None, width=896, height=512, steps=25):
        return await self._generate(prompt, [], seed=seed, steps=steps, canvas=(width, height), intent="text_master")

    async def submit_edit(self, prompt, reference_paths, *, negative_prompt="", seed=None, steps=25, intent="preserve_identity", canvas=None):
        paths = [p for p in reference_paths if p is not None]
        if not 1 <= len(paths) <= 10:
            raise ImageBackendError("INVALID_REFERENCE_COUNT", "图生图需要 1–10 张参考图。")
        if intent != "storyboard":
            prompt = "以 <image1> 为主要人物身份参考，其他参考只补充此人的角度、姿势和服装。保留人物可辨认的外貌。\n" + prompt
        return await self._generate(prompt, paths, seed=seed, steps=steps, canvas=canvas, intent=intent)
