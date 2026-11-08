"""Dedicated local Qwen image backend for MiniMax H3 Studio.

The public integration surface is intentionally small:

* ``verify_models`` checks the exact local model files.
* ``ensure_started`` connects to, or starts, only the configured image engine on
  ``127.0.0.1:8188`` and verifies the server identity.
* ``preflight`` validates every node used by one of the two registered graphs.
* ``submit_t2i`` and ``submit_edit`` are the only submission paths.  Arbitrary
  workflow JSON and caller-provided workflow paths are never accepted.
* ``poll`` / ``wait_for_completion`` / ``download_result`` handle results.
* ``free`` unloads models and ``stop_owned`` stops only a process started by
  this backend.

No inference is started at import time.  The main application can share its
``aiohttp.ClientSession`` with this class and serialize calls with its GPU lock.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import mimetypes
import os
import random
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, FormData
from PIL import Image


APP_ROOT = Path(__file__).resolve().parent
WORKFLOW_ROOT = APP_ROOT / "workflows"

T2I_WORKFLOW_ID = "qwen_image_2512_q3"
EDIT_WORKFLOW_ID = "qwen_image_edit_2511_q3_3ref"
REFERENCE_SLOT_NAMES = ("master", "pose", "detail")
EDIT_INTENTS = frozenset({"preserve_identity", "create_master", "storyboard"})

REGISTERED_WORKFLOWS: dict[str, Path] = {
    T2I_WORKFLOW_ID: WORKFLOW_ROOT / "image_t2i.api.json",
    EDIT_WORKFLOW_ID: WORKFLOW_ROOT / "image_edit_3ref.api.json",
}

WORKFLOW_REQUIRED_NODES: dict[str, frozenset[str]] = {
    T2I_WORKFLOW_ID: frozenset(
        {
            "UnetLoaderGGUF",
            "CLIPLoader",
            "VAELoader",
            "CLIPTextEncode",
            "EmptySD3LatentImage",
            "ModelSamplingAuraFlow",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        }
    ),
    EDIT_WORKFLOW_ID: frozenset(
        {
            "LoadImage",
            "CLIPLoader",
            "VAELoader",
            "UnetLoaderGGUF",
            "FluxKontextImageScale",
            "TextEncodeQwenImageEditPlus",
            "FluxKontextMultiReferenceLatentMethod",
            "VAEEncode",
            "ModelSamplingAuraFlow",
            "CFGNorm",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        }
    ),
}

MODEL_CONTRACTS: dict[str, int] = {
    "unet/qwen-image-2512-Q3_K_M.gguf": 9_932_896_800,
    "unet/qwen-image-edit-2511-Q3_K_M.gguf": 9_920_805_472,
    "text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors": 9_384_670_680,
    "vae/qwen_image_vae.safetensors": 253_806_246,
}

RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_RENDER_PIXELS = 928 * 1664
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImageBackendError(RuntimeError):
    """Structured, user-presentable backend failure."""

    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class ImageBackendConfig:
    comfy_root: Path = APP_ROOT / "runtime/legacy/ComfyUI"
    python_executable: Path = Path(
        APP_ROOT / "runtime/legacy/ComfyUI/.venv/Scripts/python.exe"
    )
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_args: tuple[str, ...] = (
        "--listen",
        "127.0.0.1",
        "--port",
        "8188",
        "--disable-auto-launch",
        "--reserve-vram",
        "1.5",
        "--cache-none",
        "--disable-all-custom-nodes",
        "--whitelist-custom-nodes",
        "ComfyUI-GGUF",
    )
    log_path: Path = Path(
        APP_ROOT / "runtime/studio_data/logs/image_comfy.log"
    )
    pid_path: Path = Path(APP_ROOT / "runtime/studio_data/image_comfy.pid")
    startup_timeout: float = 180.0
    request_timeout: float = 90.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.comfy_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("图片 ComfyUI 只允许本机 HTTP loopback 地址。")
        if parsed.port not in {8188, 8190}:
            raise ValueError("图片 ComfyUI 必须使用专用端口 8188。")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("图片 ComfyUI 地址不能包含路径、查询或片段。")


def _load_registered_graph(workflow_id: str) -> dict[str, Any]:
    path = REGISTERED_WORKFLOWS.get(workflow_id)
    if path is None:
        raise ImageBackendError(
            "WORKFLOW_NOT_REGISTERED",
            f"未注册的图片工作流：{workflow_id}",
            {"registered": sorted(REGISTERED_WORKFLOWS)},
        )
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ImageBackendError(
            "WORKFLOW_FILE_INVALID", f"无法读取已注册工作流：{path}", str(error)
        ) from error
    _validate_graph_contract(workflow_id, graph)
    return copy.deepcopy(graph)


def _validate_graph_contract(workflow_id: str, graph: Any) -> None:
    if not isinstance(graph, dict) or not graph:
        raise ImageBackendError("WORKFLOW_FILE_INVALID", "工作流不是非空 ComfyUI API 图。")
    actual_nodes: set[str] = set()
    for node_id, node in graph.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise ImageBackendError("WORKFLOW_FILE_INVALID", "工作流节点结构无效。")
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not isinstance(node.get("inputs"), dict):
            raise ImageBackendError(
                "WORKFLOW_FILE_INVALID", f"节点 {node_id} 缺少 class_type 或 inputs。"
            )
        actual_nodes.add(class_type)
    required = WORKFLOW_REQUIRED_NODES[workflow_id]
    missing = sorted(required - actual_nodes)
    if missing:
        raise ImageBackendError(
            "WORKFLOW_FILE_INVALID", "已注册工作流缺少固定节点。", {"missing": missing}
        )


def validate_registered_workflows() -> dict[str, Any]:
    """Validate registered files without contacting ComfyUI or running inference."""

    results: dict[str, Any] = {}
    for workflow_id in REGISTERED_WORKFLOWS:
        graph = _load_registered_graph(workflow_id)
        results[workflow_id] = {
            "path": str(REGISTERED_WORKFLOWS[workflow_id]),
            "nodes": len(graph),
            "class_types": sorted({node["class_type"] for node in graph.values()}),
        }
    return {"ready": True, "workflows": results}


def _validated_prompt(prompt: str, field_name: str = "prompt", max_length: int = 16_000) -> str:
    value = str(prompt).strip()
    if not value:
        raise ImageBackendError("EMPTY_PROMPT", f"{field_name} 不能为空。")
    if len(value) > max_length:
        raise ImageBackendError(
            "PROMPT_TOO_LONG", f"{field_name} 最长 {max_length} 个字符。"
        )
    return value


def _validated_negative_prompt(prompt: str) -> str:
    value = str(prompt).strip()
    if len(value) > 8_000:
        raise ImageBackendError("PROMPT_TOO_LONG", "negative_prompt 最长 8000 个字符。")
    return value


def _validated_seed(seed: int | None) -> int:
    if seed is None:
        return random.SystemRandom().randrange(0, 2**63)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**63:
        raise ImageBackendError("INVALID_SEED", "seed 必须是 0 到 2^63-1 的整数。")
    return seed


def _validated_steps(steps: int) -> int:
    if isinstance(steps, bool) or not isinstance(steps, int) or not 4 <= steps <= 40:
        raise ImageBackendError("INVALID_STEPS", "图片 steps 必须是 4 到 40 的整数。")
    return steps


def _validated_dimensions(width: int, height: int) -> tuple[int, int]:
    for label, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ImageBackendError("INVALID_DIMENSIONS", f"{label} 必须是整数。")
        if not 256 <= value <= 1664 or value % 16:
            raise ImageBackendError(
                "INVALID_DIMENSIONS", f"{label} 必须在 256–1664 之间且为 16 的倍数。"
            )
    if width * height > MAX_RENDER_PIXELS:
        raise ImageBackendError(
            "INVALID_DIMENSIONS",
            f"像素数不能超过 {MAX_RENDER_PIXELS}；高分辨率档尚未完成本机 Runtime 验收。",
        )
    return width, height


def _filename_prefix(stem: str) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-") or "image"
    return f"MiniMax-H3-Studio/{safe_stem}-{uuid.uuid4().hex[:10]}"


def _isolated_python_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for inherited_python_key in (
        "PYTHONHOME",
        "UV_INTERNAL__PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        environment.pop(inherited_python_key, None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def build_t2i_request(
    prompt: str,
    *,
    negative_prompt: str = "low quality, distorted anatomy, extra limbs, unreadable text",
    seed: int | None = None,
    width: int = 512,
    height: int = 896,
    steps: int = 12,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind user fields to the reviewed Qwen Image 2512 API graph."""

    positive = _validated_prompt(prompt)
    negative = _validated_negative_prompt(negative_prompt)
    actual_seed = _validated_seed(seed)
    actual_steps = _validated_steps(steps)
    actual_width, actual_height = _validated_dimensions(width, height)
    graph = _load_registered_graph(T2I_WORKFLOW_ID)
    graph["4"]["inputs"]["text"] = positive
    graph["5"]["inputs"]["text"] = negative
    graph["6"]["inputs"]["width"] = actual_width
    graph["6"]["inputs"]["height"] = actual_height
    graph["8"]["inputs"]["seed"] = actual_seed
    graph["8"]["inputs"]["steps"] = actual_steps
    graph["10"]["inputs"]["filename_prefix"] = _filename_prefix("Qwen-Image-2512")
    metadata = {
        "workflow_id": T2I_WORKFLOW_ID,
        "mode": "text_to_image",
        "seed": actual_seed,
        "steps": actual_steps,
        "width": actual_width,
        "height": actual_height,
        "runtime_validation": "512x896_12step_smoke_pass_only",
    }
    return graph, metadata


def build_edit_request(
    prompt: str,
    reference_names: Sequence[str | None],
    *,
    negative_prompt: str = "",
    seed: int | None = None,
    steps: int = 20,
    intent: str = "preserve_identity",
    canvas: tuple[int, int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind one to three uploaded references to the reviewed Edit 2511 graph.

    Slot 1 is always the primary identity anchor. Missing slots repeat that
    anchor; metadata records whether this request preserves a frozen master or
    creates a new candidate master from references.
    """

    positive = _validated_prompt(prompt)
    negative = _validated_negative_prompt(negative_prompt)
    if intent not in EDIT_INTENTS:
        raise ImageBackendError("INVALID_EDIT_INTENT", "未知的参考图生成用途。")
    if isinstance(reference_names, (str, bytes)) or not 1 <= len(reference_names) <= 3:
        raise ImageBackendError(
            "INVALID_REFERENCE_COUNT", "参考图生成需要 1–3 张图片，第 1 张必须是主要人物参考。"
        )
    slots = list(reference_names) + [None] * (3 - len(reference_names))
    if slots[0] is None:
        raise ImageBackendError("MISSING_MASTER_REFERENCE", "第 1 张主要人物参考不能为空。")
    normalized: list[str | None] = [
        _validated_server_input_name(name) if name is not None else None for name in slots
    ]
    master = normalized[0]
    assert master is not None
    bound: list[str] = []
    repeated_slots: list[int] = []
    supplied_slots: list[str] = []
    for index, value in enumerate(normalized):
        if value is None:
            bound.append(master)
            repeated_slots.append(index + 1)
        else:
            bound.append(value)
            supplied_slots.append(REFERENCE_SLOT_NAMES[index])

    if intent == "storyboard":
        role_lines = ["各张参考图可以属于不同人物，严格按提示词中的角色与 Picture 编号对应，不将多个人物融合成一个人。"]
        if repeated_slots:
            role_lines.append(f"Picture {', '.join(map(str, repeated_slots))} 是 Picture 1 的重复输入，不代表新增人物；出场人数和动作以分镜文字为准。")
        reference_policy = "independent_character_references"
        workflow_mode = "multi_character_storyboard"
    elif intent == "create_master":
        role_lines = [
            "Picture 1 是新角色的主要人物参考，保留稳定的面部结构、年龄、发型和辨识特征。"
        ]
        if normalized[1] is not None:
            role_lines.append("Picture 2 只补充角度、身形、构图或姿态信息。")
        if normalized[2] is not None:
            role_lines.append("Picture 3 只补充服装、画面风格或标志性细节。")
        role_lines.append(
            "只整合各图明确指定的信息，人物身份始终以 Picture 1 为准，不要平均或融合多张脸；不要拼贴或生成多人，不要复制参考背景、文字或水印；最终输出是一张新的角色母版候选。"
        )
        reference_policy = "primary_reference_repeated_for_missing_slots"
        workflow_mode = "reference_guided_master_creation"
    else:
        role_lines = ["Picture 1 是冻结身份母版，人物身份冲突时始终以 Picture 1 为准。"]
        if normalized[1] is not None:
            role_lines.append("Picture 2 只补充同一人物的侧面、全身或姿态信息。")
        if normalized[2] is not None:
            role_lines.append("Picture 3 只补充同一人物的表情、服装或标志性细节。")
        reference_policy = "frozen_master_repeated_for_missing_slots"
        workflow_mode = "multi_reference_image_edit"
    role_instruction = "多参考图规则：" + " ".join(role_lines)
    positive = f"{role_instruction}\n\n{positive}"

    actual_seed = _validated_seed(seed)
    actual_steps = _validated_steps(steps)
    graph = _load_registered_graph(EDIT_WORKFLOW_ID)
    if intent == "storyboard":
        width, height = canvas or (896, 512)
        width, height = _validated_dimensions(width, height)
        graph["12"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}
    for node_id, image_name in zip(("1", "2", "3"), bound, strict=True):
        graph[node_id]["inputs"]["image"] = image_name
    graph["8"]["inputs"]["prompt"] = positive
    graph["9"]["inputs"]["prompt"] = negative
    graph["15"]["inputs"]["seed"] = actual_seed
    graph["15"]["inputs"]["steps"] = actual_steps
    graph["17"]["inputs"]["filename_prefix"] = _filename_prefix("Qwen-Image-Edit-2511")
    metadata = {
        "workflow_id": EDIT_WORKFLOW_ID,
        "mode": workflow_mode,
        "reference_intent": intent,
        "seed": actual_seed,
        "steps": actual_steps,
        "primary_reference": master,
        "master_reference": master if intent == "preserve_identity" else None,
        "supplied_reference_count": len(supplied_slots),
        "supplied_reference_slots": supplied_slots,
        "bound_reference_names": bound,
        "repeated_master_slots": repeated_slots,
        "role_instruction": role_instruction,
        "reference_policy": reference_policy,
        "runtime_validation": (
            "single_master_repeat_and_three_distinct_cutout_refs_12step_pass_current_machine"
            if intent == "preserve_identity"
            else "multi_character_storyboard_requires_per_job_validation" if intent == "storyboard"
            else "one_primary_reference_create_master_800x1328_12step_runtime_pass_current_machine"
        ),
    }
    return graph, metadata


def _validated_server_input_name(name: str) -> str:
    value = str(name).strip().replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ImageBackendError("INVALID_UPLOAD_NAME", "ComfyUI 输入文件名不安全。")
    return path.as_posix()


def _find_image_asset(value: Any) -> dict[str, str] | None:
    if isinstance(value, Mapping):
        images = value.get("images")
        if isinstance(images, list):
            for item in images:
                if isinstance(item, Mapping):
                    filename = str(item.get("filename", ""))
                    if Path(filename).suffix.lower() in RASTER_SUFFIXES:
                        return {
                            "filename": filename,
                            "subfolder": str(item.get("subfolder", "")),
                            "type": str(item.get("type", "output")),
                        }
        for nested in value.values():
            found = _find_image_asset(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_image_asset(nested)
            if found:
                return found
    return None


@dataclass
class ImageBackend:
    """Managed connector for the dedicated Qwen-image ComfyUI instance."""

    session: ClientSession
    config: ImageBackendConfig = field(default_factory=ImageBackendConfig)
    _process: subprocess.Popen[Any] | None = field(default=None, init=False, repr=False)
    _log_handle: Any | None = field(default=None, init=False, repr=False)
    _owned: bool = field(default=False, init=False)
    _start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def owned(self) -> bool:
        return self._owned

    @property
    def base_url(self) -> str:
        return self.config.comfy_url.rstrip("/")

    @property
    def input_root(self) -> Path:
        return self.config.comfy_root / "input"

    def verify_models(self) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for relative_name, expected_size in MODEL_CONTRACTS.items():
            path = self.config.comfy_root / "models" / Path(relative_name)
            actual_size = path.stat().st_size if path.is_file() else 0
            files.append(
                {
                    "path": str(path),
                    "relative_path": relative_name,
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                    "ready": actual_size == expected_size,
                }
            )
        return {"ready": all(item["ready"] for item in files), "files": files}

    async def health(self) -> tuple[bool, dict[str, Any] | None]:
        try:
            async with self.session.get(
                f"{self.base_url}/system_stats", timeout=ClientTimeout(total=4)
            ) as response:
                if response.status != 200:
                    return False, None
                payload = await response.json(content_type=None)
                return isinstance(payload, dict), payload if isinstance(payload, dict) else None
        except (ClientError, OSError, asyncio.TimeoutError, ValueError):
            return False, None

    def _assert_server_identity(self, stats: Mapping[str, Any] | None) -> None:
        argv = ((stats or {}).get("system") or {}).get("argv") or []
        expected = os.path.normcase(str((self.config.comfy_root / "main.py").resolve()))
        actual_paths: list[str] = []
        for argument in argv:
            value = str(argument)
            if value.lower().endswith("main.py"):
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = self.config.comfy_root / candidate
                actual_paths.append(os.path.normcase(str(candidate.resolve())))
        if expected not in actual_paths:
            raise ImageBackendError(
                "PORT_IDENTITY_MISMATCH",
                "8188 端口已被另一服务占用，拒绝把图片发给错误的 ComfyUI。",
                {"argv": [str(item) for item in argv]},
            )

    async def ensure_started(self) -> dict[str, Any]:
        async with self._start_lock:
            model_state = self.verify_models()
            if not model_state["ready"]:
                missing = [item for item in model_state["files"] if not item["ready"]]
                raise ImageBackendError(
                    "PREFLIGHT_MISSING_MODELS", "本地 Qwen 图片模型缺失或大小不符。", missing
                )

            healthy, stats = await self.health()
            if healthy and stats:
                self._assert_server_identity(stats)
                if not self._owned:
                    self._owned = False
                return stats

            main_py = self.config.comfy_root / "main.py"
            if not main_py.is_file() or not self.config.python_executable.is_file():
                raise ImageBackendError(
                    "SERVER_UNAVAILABLE",
                    "找不到专用图片 ComfyUI 或 Python。",
                    {"main_py": str(main_py), "python": str(self.config.python_executable)},
                )

            self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.config.log_path.open("a", encoding="utf-8")
            environment = _isolated_python_environment()
            # Studio itself runs inside a uv-managed Python 3.13 venv whose
            # launcher injects PYTHONHOME.  Carrying that into the user's
            # separate Python 3.12 image runtime causes an _sre ABI mismatch.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            command = [
                str(self.config.python_executable),
                str(main_py),
                *self.config.comfy_args,
            ]
            self._process = subprocess.Popen(
                command,
                cwd=self.config.comfy_root,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=creationflags,
            )
            self._owned = True
            self.config.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.pid_path.write_text(str(self._process.pid), encoding="ascii")
            deadline = time.monotonic() + self.config.startup_timeout
            try:
                while time.monotonic() < deadline:
                    if self._process.poll() is not None:
                        raise ImageBackendError(
                            "SERVER_START_FAILED",
                            f"图片 ComfyUI 启动失败，退出码 {self._process.returncode}。",
                            {"log": str(self.config.log_path)},
                        )
                    healthy, stats = await self.health()
                    if healthy and stats:
                        self._assert_server_identity(stats)
                        return stats
                    await asyncio.sleep(2)
                raise ImageBackendError(
                    "SERVER_START_TIMEOUT",
                    f"图片 ComfyUI 在 {int(self.config.startup_timeout)} 秒内没有就绪。",
                    {"log": str(self.config.log_path)},
                )
            except Exception:
                await self.stop_owned(free_memory=False)
                raise

    async def validate_nodes(self, workflow_id: str | None = None) -> dict[str, Any]:
        workflow_ids = [workflow_id] if workflow_id else list(REGISTERED_WORKFLOWS)
        for candidate in workflow_ids:
            if candidate not in REGISTERED_WORKFLOWS:
                raise ImageBackendError(
                    "WORKFLOW_NOT_REGISTERED", f"未注册的图片工作流：{candidate}"
                )
        required = set().union(*(WORKFLOW_REQUIRED_NODES[item] for item in workflow_ids))
        missing: list[str] = []
        for node_type in sorted(required):
            try:
                async with self.session.get(
                    f"{self.base_url}/object_info/{node_type}",
                    timeout=ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        missing.append(node_type)
                        continue
                    payload = await response.json(content_type=None)
                    if not isinstance(payload, dict) or node_type not in payload:
                        missing.append(node_type)
            except (ClientError, OSError, asyncio.TimeoutError, ValueError):
                missing.append(node_type)
        return {"ready": not missing, "checked": sorted(required), "missing": missing}

    async def preflight(self, workflow_id: str | None = None) -> dict[str, Any]:
        workflow_state = validate_registered_workflows()
        model_state = self.verify_models()
        stats = await self.ensure_started()
        node_state = await self.validate_nodes(workflow_id)
        if not node_state["ready"]:
            raise ImageBackendError(
                "PREFLIGHT_MISSING_NODES", "图片 ComfyUI 缺少工作流节点。", node_state["missing"]
            )
        return {
            "ready": True,
            "server": {"url": self.base_url, "owned": self._owned, "stats": stats},
            "models": model_state,
            "nodes": node_state,
            "workflows": workflow_state,
        }

    async def upload_image(self, source: str | Path) -> dict[str, Any]:
        path = Path(source).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in RASTER_SUFFIXES:
            raise ImageBackendError(
                "INPUT_IMAGE_NOT_FOUND", f"找不到可用的 PNG/JPG/WEBP 图片：{path}"
            )
        size = path.stat().st_size
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise ImageBackendError(
                "INVALID_INPUT_IMAGE", f"参考图必须在 1 byte 到 {MAX_UPLOAD_BYTES} bytes 之间。"
            )
        try:
            with Image.open(path) as image:
                image.verify()
                width, height = image.size
        except Exception as error:
            raise ImageBackendError("INVALID_INPUT_IMAGE", f"参考图无法解码：{path}") from error

        digest = hashlib.sha256()
        with path.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)

        unique_name = f"h3studio-{uuid.uuid4().hex[:12]}-{re.sub(r'[^A-Za-z0-9._-]+', '-', path.name)}"
        form = FormData()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as source_file:
                form.add_field(
                    "image", source_file, filename=unique_name, content_type=content_type
                )
                form.add_field("type", "input")
                form.add_field("subfolder", "minimax_h3_studio")
                form.add_field("overwrite", "false")
                async with self.session.post(
                    f"{self.base_url}/upload/image",
                    data=form,
                    timeout=ClientTimeout(total=300),
                ) as response:
                    payload = await _read_response_payload(response)
                    if response.status not in {200, 201} or not isinstance(payload, dict):
                        raise ImageBackendError(
                            "UPLOAD_FAILED",
                            "上传参考图到本地图片 ComfyUI 失败。",
                            {"status": response.status, "response": payload},
                        )
        except ImageBackendError:
            raise
        except (ClientError, OSError, asyncio.TimeoutError) as error:
            raise ImageBackendError(
                "SERVER_UNAVAILABLE", f"无法连接本地图片 ComfyUI：{error}"
            ) from error

        name = str(payload.get("name", unique_name))
        subfolder = str(payload.get("subfolder", "minimax_h3_studio")).replace("\\", "/")
        input_name = _validated_server_input_name(
            PurePosixPath(subfolder, name).as_posix() if subfolder else name
        )
        return {
            "source_path": str(path),
            "source_sha256": digest.hexdigest(),
            "source_size": size,
            "width": width,
            "height": height,
            "name": name,
            "subfolder": subfolder,
            "type": str(payload.get("type", "input")),
            "input_name": input_name,
        }

    async def submit_t2i(
        self,
        prompt: str,
        *,
        negative_prompt: str = "low quality, distorted anatomy, extra limbs, unreadable text",
        seed: int | None = None,
        width: int = 512,
        height: int = 896,
        steps: int = 12,
    ) -> dict[str, Any]:
        await self.preflight(T2I_WORKFLOW_ID)
        graph, metadata = build_t2i_request(
            prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            width=width,
            height=height,
            steps=steps,
        )
        submission = await self._submit_graph(graph)
        submission["metadata"] = metadata
        submission["uploads"] = []
        return submission

    async def submit_edit(
        self,
        prompt: str,
        reference_paths: Sequence[str | Path | None],
        *,
        negative_prompt: str = "",
        seed: int | None = None,
        steps: int = 20,
        intent: str = "preserve_identity",
        canvas: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        if isinstance(reference_paths, (str, bytes, Path)) or not 1 <= len(reference_paths) <= 3:
            raise ImageBackendError(
                "INVALID_REFERENCE_COUNT", "参考图生成需要 1–3 张图片，第 1 张必须是主要人物参考。"
            )
        slots = list(reference_paths) + [None] * (3 - len(reference_paths))
        if slots[0] is None:
            raise ImageBackendError("MISSING_MASTER_REFERENCE", "第 1 张主要人物参考不能为空。")
        if intent not in EDIT_INTENTS:
            raise ImageBackendError("INVALID_EDIT_INTENT", "未知的参考图生成用途。")
        await self.preflight(EDIT_WORKFLOW_ID)
        uploads: list[dict[str, Any]] = []
        uploaded_slots: list[dict[str, Any] | None] = [None, None, None]
        try:
            for index, reference_path in enumerate(slots):
                if reference_path is None:
                    continue
                upload = await self.upload_image(reference_path)
                upload["reference_slot"] = REFERENCE_SLOT_NAMES[index]
                uploads.append(upload)
                uploaded_slots[index] = upload
            graph, metadata = build_edit_request(
                prompt,
                [upload["input_name"] if upload else None for upload in uploaded_slots],
                negative_prompt=negative_prompt,
                seed=seed,
                steps=steps,
                intent=intent,
                canvas=canvas,
            )
            submission = await self._submit_graph(graph)
        except asyncio.CancelledError:
            self.cleanup_uploads(uploads)
            raise
        except Exception:
            self.cleanup_uploads(uploads)
            raise
        submission["metadata"] = metadata
        submission["uploads"] = uploads
        return submission

    async def _submit_graph(self, graph: dict[str, Any]) -> dict[str, Any]:
        client_id = uuid.uuid4().hex
        try:
            async with self.session.post(
                f"{self.base_url}/prompt",
                json={"prompt": graph, "client_id": client_id},
                timeout=ClientTimeout(total=self.config.request_timeout),
            ) as response:
                payload = await _read_response_payload(response)
                if response.status != 200 or not isinstance(payload, dict):
                    raise ImageBackendError(
                        "PROMPT_REJECTED",
                        "本地图片 ComfyUI 拒绝了已注册工作流。",
                        {"status": response.status, "response": payload},
                    )
        except ImageBackendError:
            raise
        except (ClientError, OSError, asyncio.TimeoutError) as error:
            raise ImageBackendError(
                "SERVER_UNAVAILABLE", f"无法连接本地图片 ComfyUI：{error}"
            ) from error
        prompt_id = str(payload.get("prompt_id", ""))
        if not prompt_id:
            raise ImageBackendError("PROMPT_REJECTED", "ComfyUI 没有返回 prompt_id。", payload)
        return {
            "prompt_id": prompt_id,
            "client_id": client_id,
            "status": "queued",
            "number": payload.get("number"),
            "node_errors": payload.get("node_errors") or {},
        }

    async def poll(self, prompt_id: str) -> dict[str, Any]:
        value = str(prompt_id).strip()
        if not value:
            raise ImageBackendError("INVALID_PROMPT_ID", "prompt_id 不能为空。")
        try:
            async with self.session.get(
                f"{self.base_url}/history/{value}", timeout=ClientTimeout(total=20)
            ) as response:
                history_payload = await _read_response_payload(response)
                if response.status != 200:
                    raise ImageBackendError(
                        "POLL_FAILED", "读取图片任务历史失败。", history_payload
                    )
            if isinstance(history_payload, dict) and value in history_payload:
                record = history_payload[value]
                status = record.get("status") if isinstance(record, dict) else {}
                status_text = str((status or {}).get("status_str", "")).lower()
                if status_text in {"error", "failed"}:
                    raise ImageBackendError(
                        "COMFY_EXECUTION_FAILED",
                        _history_error_message(status) or "图片 ComfyUI 执行失败。",
                        status,
                    )
                asset = _find_image_asset(record.get("outputs", {})) if isinstance(record, dict) else None
                if asset:
                    return {
                        "prompt_id": value,
                        "status": "completed",
                        "asset": asset,
                        "history": record,
                    }
                if bool((status or {}).get("completed")):
                    raise ImageBackendError(
                        "NO_OUTPUT", "图片工作流已结束，但历史中没有 PNG/JPG/WEBP 输出。"
                    )

            async with self.session.get(
                f"{self.base_url}/queue", timeout=ClientTimeout(total=20)
            ) as response:
                queue_payload = await _read_response_payload(response)
                if response.status != 200 or not isinstance(queue_payload, dict):
                    raise ImageBackendError("POLL_FAILED", "读取图片任务队列失败。", queue_payload)
            running_ids = _queue_prompt_ids(queue_payload.get("queue_running", []))
            pending_ids = _queue_prompt_ids(queue_payload.get("queue_pending", []))
            state = "running" if value in running_ids else "queued"
            if value not in running_ids and value not in pending_ids:
                state = "waiting_for_history"
            return {"prompt_id": value, "status": state}
        except ImageBackendError:
            raise
        except (ClientError, OSError, asyncio.TimeoutError) as error:
            raise ImageBackendError(
                "SERVER_UNAVAILABLE", f"无法连接本地图片 ComfyUI：{error}"
            ) from error

    async def wait_for_completion(
        self, prompt_id: str, *, timeout: float = 3600.0, interval: float = 2.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.poll(prompt_id)
            if state["status"] == "completed":
                return state
            await asyncio.sleep(interval)
        raise ImageBackendError(
            "GENERATION_TIMEOUT", f"图片任务在 {int(timeout)} 秒内没有完成。"
        )

    async def download_result(
        self, asset: Mapping[str, Any], output_dir: str | Path
    ) -> dict[str, Any]:
        filename = str(asset.get("filename", ""))
        suffix = Path(filename).suffix.lower()
        if suffix not in RASTER_SUFFIXES:
            raise ImageBackendError("INVALID_OUTPUT", "ComfyUI 返回的不是受支持的图片。")
        destination_root = Path(output_dir).expanduser().resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / f"{uuid.uuid4().hex[:12]}-{Path(filename).name}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        params = {
            "filename": filename,
            "subfolder": str(asset.get("subfolder", "")),
            "type": str(asset.get("type", "output")),
        }
        digest = hashlib.sha256()
        size = 0
        try:
            async with self.session.get(
                f"{self.base_url}/view", params=params, timeout=ClientTimeout(total=300)
            ) as response:
                if response.status != 200:
                    payload = await _read_response_payload(response)
                    raise ImageBackendError(
                        "DOWNLOAD_FAILED",
                        "下载本地 ComfyUI 图片结果失败。",
                        {"status": response.status, "response": payload},
                    )
                with temporary.open("wb") as output_file:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        output_file.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"path": str(destination), "size": size, "sha256": digest.hexdigest()}

    async def free(self) -> bool:
        healthy, _ = await self.health()
        if not healthy:
            return False
        try:
            async with self.session.post(
                f"{self.base_url}/free",
                json={"unload_models": True, "free_memory": True},
                timeout=ClientTimeout(total=60),
            ) as response:
                return response.status in {200, 204}
        except (ClientError, OSError, asyncio.TimeoutError):
            return False

    def cleanup_uploads(self, uploads: Iterable[Mapping[str, Any]]) -> list[str]:
        removed: list[str] = []
        input_root = self.input_root.resolve()
        for upload in uploads:
            try:
                input_name = _validated_server_input_name(str(upload.get("input_name", "")))
                candidate = (input_root / Path(*PurePosixPath(input_name).parts)).resolve()
                candidate.relative_to(input_root)
                if candidate.is_file():
                    candidate.unlink()
                    removed.append(str(candidate))
            except (ImageBackendError, OSError, ValueError):
                continue
        return removed

    async def stop_owned(self, *, free_memory: bool = True) -> None:
        if not self._owned:
            return
        if free_memory:
            await self.free()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(lambda: process.wait(timeout=20))
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        if self._log_handle is not None:
            self._log_handle.close()
        self._process = None
        self._log_handle = None
        self._owned = False
        self.config.pid_path.unlink(missing_ok=True)


async def _read_response_payload(response: Any) -> Any:
    try:
        return await response.json(content_type=None)
    except (ValueError, TypeError):
        return await response.text()


def _queue_prompt_ids(entries: Any) -> set[str]:
    results: set[str] = set()
    if not isinstance(entries, list):
        return results
    for entry in entries:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            results.add(str(entry[1]))
        elif isinstance(entry, Mapping) and entry.get("prompt_id"):
            results.add(str(entry["prompt_id"]))
    return results


def _history_error_message(status: Any) -> str:
    if not isinstance(status, Mapping):
        return ""
    messages = status.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        payload = message[1]
        if isinstance(payload, Mapping):
            for key in ("exception_message", "exception_type", "error"):
                if payload.get(key):
                    return str(payload[key])
    return ""


__all__ = [
    "EDIT_WORKFLOW_ID",
    "ImageBackend",
    "ImageBackendConfig",
    "ImageBackendError",
    "REGISTERED_WORKFLOWS",
    "T2I_WORKFLOW_ID",
    "build_edit_request",
    "build_t2i_request",
    "validate_registered_workflows",
]
