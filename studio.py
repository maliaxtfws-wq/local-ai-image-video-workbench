from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import io
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import uuid
import webbrowser
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, FormData, WSMsgType, web
from PIL import Image, ImageOps

from image_backend import (
    ImageBackend,
    ImageBackendConfig,
    ImageBackendError,
    REFERENCE_SLOT_NAMES,
    _isolated_python_environment,
)
from local_prompt_llm import LocalPromptLLM, PromptLLMError
from story_sequences import StorySequences, joined_thread
from storyboards import Storyboards, identifier
from storyboard_batches import StoryboardBatches
import image_output
from qwen21_backend import Qwen21Backend
from batch_images import MAX_SOURCE_BYTES, MAX_IMPORT_BYTES, prepare_photo, remove_cached


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
WORKFLOW_ROOT = APP_ROOT / "workflows"
def read_config():
    path = APP_ROOT / "config.json"
    if not path.is_file():
        path = APP_ROOT / "config.example.json"
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    def resolve(value):
        return str((APP_ROOT / Path(value).expanduser()).resolve())
    for key in ("runtime_root", "video_comfy_root", "video_comfy_python", "image_comfy_root",
                "image_comfy_python", "qwen21_comfy_root", "qwen21_comfy_python",
                "portrait_cutout_model", "ollama_executable", "ollama_models"):
        config[key] = resolve(config[key])
    config["batch_import_roots"] = [resolve(value) for value in config["batch_import_roots"]]
    return config

CONFIG = read_config()
MANIFEST = json.loads((WORKFLOW_ROOT / "manifest.json").read_text(encoding="utf-8"))
MODEL_MANIFEST = json.loads((APP_ROOT / "models_manifest.json").read_text(encoding="utf-8"))

RUNTIME_ROOT = Path(CONFIG["runtime_root"])
COMFY_ROOT = Path(CONFIG["video_comfy_root"])
COMFY_PYTHON = Path(CONFIG["video_comfy_python"])
COMFY_URL = CONFIG["comfy_url"].rstrip("/")
DATA_ROOT = RUNTIME_ROOT / "studio_data"
UPLOAD_ROOT = DATA_ROOT / "uploads"
BATCH_CACHE_ROOT = DATA_ROOT / "batch_imports"
RESULT_ROOT = DATA_ROOT / "results"
LOG_ROOT = DATA_ROOT / "logs"
PID_FILE = DATA_ROOT / "studio.pid"
COMFY_PID_FILE = DATA_ROOT / "comfy.pid"
IMAGE_COMFY_URL = CONFIG["image_comfy_url"].rstrip("/")
IMAGE_COMFY_ROOT = Path(CONFIG["image_comfy_root"])
CUTOUT_MODEL = Path(CONFIG["portrait_cutout_model"])
CUTOUT_PYTHON = Path(CONFIG["image_comfy_python"])
CUTOUT_WORKER = APP_ROOT / "portrait_cutout_worker.py"
CUTOUT_MODEL_SIZE = 972_666_916
CUTOUT_MODEL_SHA256 = "58f621f00f5d756097615970a88a791584600dcf7c45b18a0a6267535a1ebd3c"

ACTIVE_STATUSES = {"starting", "uploading", "queued", "running", "saving", "cancelling"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
VIDEO_FPS = 24
VIDEO_FRAMES = 124
CONTINUATION_GUIDE_FRAMES = 22
BATCH_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
BATCH_SELECTION_MAX_FILES = 200
BATCH_SELECTION_TTL_SECONDS = 12 * 60 * 60
BATCH_SELECTION_MAX_REGISTRIES = 8
BATCH_SCAN_MAX_ENTRIES = 5000
BATCH_SCAN_MAX_DEPTH = 8
WINDOWS_RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
REQUIRED_NODE_TYPES = {
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "MiniMaxH3ImageToVideo",
    "RandomNoise",
    "KSamplerSelect",
    "BasicScheduler",
    "BasicGuider",
    "SamplerCustomAdvanced",
    "VAEDecode",
    "VAEDecodeAudio",
    "CreateVideo",
    "SaveVideo",
    "LoadImage",
    "ImageScale",
    "LoraLoaderModelOnly",
    "LoadVideo",
    "GetVideoComponents",
    "MiniMaxH3AddGuide",
}
Image.MAX_IMAGE_PIXELS = 40_000_000

PROMPT_PROFILE_IDS = frozenset({"stable", "creative"})
OLLAMA_MODEL_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_prompt_model_profiles(config: dict[str, Any]) -> tuple[str, dict[str, dict[str, str]]]:
    """Validate the two server-owned prompt profiles and their model tags."""

    raw_profiles = config.get("ollama_model_profiles")
    if not isinstance(raw_profiles, dict) or set(raw_profiles) != PROMPT_PROFILE_IDS:
        raise ValueError("ollama_model_profiles must define exactly stable and creative")
    profiles: dict[str, dict[str, str]] = {}
    for profile_id in sorted(PROMPT_PROFILE_IDS):
        raw = raw_profiles[profile_id]
        if not isinstance(raw, dict):
            raise ValueError(f"prompt profile {profile_id} must be an object")
        model = str(raw.get("model", ""))
        label = str(raw.get("label", "")).strip()
        manifest_sha256 = str(raw.get("manifest_sha256", "")).lower()
        system_profile = str(raw.get("system_profile", ""))
        ollama_manifest_path(Path(config["ollama_models"]), model)
        if not label or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) or system_profile != profile_id:
            raise ValueError(f"prompt profile {profile_id} has invalid label or system_profile")
        profiles[profile_id] = {
            "model": model,
            "label": label,
            "manifest_sha256": manifest_sha256,
            "system_profile": system_profile,
        }
    default_profile = str(config.get("ollama_default_profile", "stable"))
    if default_profile not in profiles:
        raise ValueError("ollama_default_profile is not registered")
    if str(config.get("ollama_model", "")) != profiles[default_profile]["model"]:
        raise ValueError("ollama_model must match the default prompt profile")
    return default_profile, profiles


def ollama_manifest_path(models_root: Path, model: str) -> Path:
    """Resolve an allowlisted Ollama tag to its local manifest without traversal."""

    if model.count(":") != 1:
        raise ValueError("Ollama model must include exactly one tag")
    repository, tag = model.rsplit(":", 1)
    repository_parts = repository.split("/")
    if len(repository_parts) == 1:
        namespace, name = "library", repository_parts[0]
    elif len(repository_parts) == 2:
        namespace, name = repository_parts
    else:
        raise ValueError("Ollama model repository must be name or namespace/name")
    components = (namespace, name, tag)
    if any(not OLLAMA_MODEL_COMPONENT.fullmatch(component) for component in components):
        raise ValueError("Ollama model contains an unsafe path component")
    return models_root / "manifests" / "registry.ollama.ai" / namespace / name / tag


def ollama_model_installation(models_root: Path, model: str, expected_manifest_sha256: str) -> dict[str, Any]:
    """Verify that an Ollama manifest and every content-addressed blob are complete."""

    manifest_path = ollama_manifest_path(models_root, model)
    if not manifest_path.is_file():
        return {"installed": False, "model_size_bytes": 0}
    try:
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha256:
            return {"installed": False, "model_size_bytes": 0}
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"installed": False, "model_size_bytes": 0}
    descriptors = [manifest.get("config"), *(manifest.get("layers") or [])]
    if not descriptors or any(not isinstance(item, dict) for item in descriptors):
        return {"installed": False, "model_size_bytes": 0}
    model_size = 0
    for descriptor in descriptors:
        digest = descriptor.get("digest")
        size = descriptor.get("size")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or size < 0
        ):
            return {"installed": False, "model_size_bytes": 0}
        blob = models_root / "blobs" / digest.replace(":", "-", 1)
        if not blob.is_file() or blob.stat().st_size != size:
            return {"installed": False, "model_size_bytes": 0}
        if descriptor.get("mediaType") == "application/vnd.ollama.image.model":
            model_size += size
    return {"installed": model_size > 0, "model_size_bytes": model_size}


DEFAULT_PROMPT_PROFILE, PROMPT_MODEL_PROFILES = validate_prompt_model_profiles(CONFIG)
_MISSING_PROMPT_PROFILE = object()


def resolve_prompt_model_profile(value: object = _MISSING_PROMPT_PROFILE) -> tuple[str, dict[str, str]]:
    profile_id = DEFAULT_PROMPT_PROFILE if value is _MISSING_PROMPT_PROFILE else value
    if not isinstance(profile_id, str):
        raise TypeError("prompt profile must be text")
    try:
        return profile_id, PROMPT_MODEL_PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError("prompt profile is not registered") from exc


def json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False))


def compose_character_prompt(prompt: str, character_lock: str) -> str:
    """Bind a user-owned identity lock without asking the model to rewrite it."""

    if not character_lock:
        return prompt
    return f"人物固定描述（必须保持，不得改写）：{character_lock}\n\n当前画面与动作：{prompt}"


def prompt_install_state(manager: LocalPromptLLM) -> dict[str, Any]:
    health = manager.health_check(timeout=0.5)
    installed_models = set(health.get("installed_models", [])) if health.get("ok") else set()
    runtime_present = manager.executable.is_file() and manager.models_dir.is_dir()
    profile_states: dict[str, dict[str, Any]] = {}
    for profile_id, profile in PROMPT_MODEL_PROFILES.items():
        installation = ollama_model_installation(
            manager.models_dir,
            profile["model"],
            profile["manifest_sha256"],
        )
        installed = runtime_present and installation["installed"]
        profile_states[profile_id] = {
            "label": profile["label"],
            "model": profile["model"],
            "installed": installed,
            "model_ready": profile["model"] in installed_models if health.get("ok") else installed,
            "model_size_bytes": installation["model_size_bytes"],
        }
    default_state = profile_states[DEFAULT_PROMPT_PROFILE]
    return {
        "installed": default_state["installed"],
        "service_healthy": bool(health.get("ok")),
        "model_ready": default_state["model_ready"],
        "model": default_state["model"],
        "version": health.get("version"),
        "starts_on_demand": True,
        "default_profile": DEFAULT_PROMPT_PROFILE,
        "profiles": profile_states,
    }


def image_model_state() -> dict[str, Any]:
    required = [
        IMAGE_COMFY_ROOT / "models" / "unet" / "qwen-image-2512-Q3_K_M.gguf",
        IMAGE_COMFY_ROOT / "models" / "unet" / "qwen-image-edit-2511-Q3_K_M.gguf",
        IMAGE_COMFY_ROOT / "models" / "text_encoders" / "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        IMAGE_COMFY_ROOT / "models" / "vae" / "qwen_image_vae.safetensors",
    ]
    files = [{"name": path.name, "present": path.is_file(), "size": path.stat().st_size if path.is_file() else 0} for path in required]
    cutout_size = CUTOUT_MODEL.stat().st_size if CUTOUT_MODEL.is_file() else 0
    return {
        "ready": all(item["present"] for item in files),
        "files": files,
        "starts_on_demand": True,
        "cutout": {
            "ready": CUTOUT_PYTHON.is_file()
            and CUTOUT_WORKER.is_file()
            and cutout_size == CUTOUT_MODEL_SIZE,
            "model": "BiRefNet-General ONNX",
            "path": str(CUTOUT_MODEL),
            "expected_size": CUTOUT_MODEL_SIZE,
            "actual_size": cutout_size,
            "provider": "CPUExecutionProvider",
        },
    }


async def free_comfy_models(session: ClientSession, base_url: str) -> bool:
    try:
        async with session.post(
            f"{base_url}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=ClientTimeout(total=30),
        ) as response:
            return response.status == 200
    except (ClientError, OSError, asyncio.TimeoutError):
        return False


def model_state() -> dict[str, Any]:
    files = []
    missing_required = []
    for entry in MODEL_MANIFEST["files"]:
        path = COMFY_ROOT / "models" / Path(entry["path"])
        actual_size = path.stat().st_size if path.is_file() else 0
        complete = actual_size == entry["size"]
        record = {
            "path": entry["path"],
            "required": entry["required"],
            "complete": complete,
            "expected_size": entry["size"],
            "actual_size": actual_size,
        }
        files.append(record)
        if entry["required"] and not complete:
            missing_required.append(entry["path"])
    return {"ready": not missing_required, "missing_required": missing_required, "files": files}


def verify_required_models() -> dict[str, Any]:
    results = []
    for entry in MODEL_MANIFEST["files"]:
        if not entry["required"]:
            continue
        path = COMFY_ROOT / "models" / Path(entry["path"])
        if not path.is_file() or path.stat().st_size != entry["size"]:
            results.append({"path": entry["path"], "ok": False, "reason": "missing_or_wrong_size"})
            continue
        digest = hashlib.sha256()
        with path.open("rb") as model_file:
            while chunk := model_file.read(8 * 1024 * 1024):
                digest.update(chunk)
        actual = digest.hexdigest()
        results.append({"path": entry["path"], "ok": actual == entry["sha256"], "sha256": actual})
    return {"ready": bool(results) and all(result["ok"] for result in results), "files": results}


async def comfy_health(session: ClientSession) -> tuple[bool, dict[str, Any] | None]:
    try:
        async with session.get(f"{COMFY_URL}/system_stats", timeout=ClientTimeout(total=4)) as response:
            if response.status != 200:
                return False, None
            return True, await response.json()
    except (OSError, asyncio.TimeoutError):
        return False, None


async def wait_for_comfy(session: ClientSession, process: subprocess.Popen[Any], timeout: float = 180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"ComfyUI 启动失败，退出码 {process.returncode}。请查看 {LOG_ROOT / 'comfy.log'}")
        healthy, stats = await comfy_health(session)
        if healthy and stats:
            return stats
        await asyncio.sleep(2)
    raise RuntimeError(f"ComfyUI 在 {int(timeout)} 秒内没有就绪。请查看 {LOG_ROOT / 'comfy.log'}")


async def ensure_comfy(app: web.Application) -> None:
    session: ClientSession = app["session"]
    healthy, stats = await comfy_health(session)
    if healthy:
        argv = ((stats or {}).get("system") or {}).get("argv") or []
        expected_main = str((COMFY_ROOT / "main.py").resolve()).lower()
        if not any(str(argument).lower() == expected_main for argument in argv):
            raise RuntimeError(f"专用端口 8189 已被另一个服务占用，拒绝误连。实际 argv：{argv}")
        app["comfy_owned"] = False
        app["comfy_stats"] = stats
        return

    if not COMFY_PYTHON.is_file() or not (COMFY_ROOT / "main.py").is_file():
        raise RuntimeError(f"找不到隔离的 ComfyUI 环境：{COMFY_ROOT}")

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_handle = (LOG_ROOT / "comfy.log").open("a", encoding="utf-8")
    command = [str(COMFY_PYTHON), str(COMFY_ROOT / "main.py"), *CONFIG["comfy_args"]]
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        cwd=COMFY_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=environment,
        creationflags=creationflags,
    )
    app["comfy_process"] = process
    app["comfy_log_handle"] = log_handle
    app["comfy_owned"] = True
    COMFY_PID_FILE.write_text(str(process.pid), encoding="ascii")
    try:
        app["comfy_stats"] = await wait_for_comfy(session, process)
    except Exception:
        process.terminate()
        try:
            await asyncio.to_thread(lambda: process.wait(timeout=20))
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        log_handle.close()
        app["comfy_process"] = None
        app["comfy_log_handle"] = None
        app["comfy_owned"] = False
        COMFY_PID_FILE.unlink(missing_ok=True)
        raise


async def validate_nodes(session: ClientSession) -> list[str]:
    missing = []
    for node_type in sorted(REQUIRED_NODE_TYPES):
        try:
            async with session.get(f"{COMFY_URL}/object_info/{node_type}", timeout=ClientTimeout(total=15)) as response:
                if response.status != 200:
                    missing.append(node_type)
                    continue
                payload = await response.json()
                if node_type not in payload:
                    missing.append(node_type)
        except (OSError, asyncio.TimeoutError):
            missing.append(node_type)
    return missing


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "stage",
        "progress_value",
        "progress_max",
        "workflow",
        "quality",
        "aspect",
        "width",
        "height",
        "seed",
        "error",
        "video_url",
        "filename",
        "media",
        "continuation",
        "continuation_strategy",
        "overlap_frames",
        "created_at",
        "completed_at",
        "batch_item_id",
        "batch_image_preparation",
        "sequence_id",
        "shot_index",
    )
    return {key: job.get(key) for key in keys if key in job}


def active_job(app: web.Application) -> dict[str, Any] | None:
    for job in app["jobs"].values():
        if job.get("status") in ACTIVE_STATUSES:
            return job
    return None


def public_image_job(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "stage",
        "progress_value",
        "progress_max",
        "mode",
        "generation_route",
        "image_engine",
        "quality",
        "aspect",
        "width",
        "height",
        "seed",
        "error",
        "image_url",
        "filename",
        "reference_policy",
        "reference_preprocess",
        "batch_item_id",
        "batch_relative_path",
        "batch_image_preparation",
        "created_at",
        "completed_at",
    )
    return {key: job.get(key) for key in keys if key in job}


def active_image_job(app: web.Application) -> dict[str, Any] | None:
    for job in app["image_jobs"].values():
        if job.get("status") in ACTIVE_STATUSES:
            return job
    return None


def active_gpu_job(app: web.Application) -> dict[str, Any] | None:
    sequences = app.get("sequences")
    batches = app.get("storyboard_batches")
    return active_job(app) or active_image_job(app) or (sequences.active() if sequences else None) or (batches.active() if batches else None)


def choose_aspect(requested: str, image_size: tuple[int, int] | None) -> str:
    if requested in {"landscape", "portrait", "square"}:
        return requested
    if not image_size:
        return "landscape"
    width, height = image_size
    if height > width * 1.1:
        return "portrait"
    if width > height * 1.1:
        return "landscape"
    return "square"


def batch_natural_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def batch_import_roots() -> tuple[Path, ...]:
    configured = CONFIG.get("batch_import_roots", [])
    if not isinstance(configured, list) or not configured:
        raise ValueError("工作台尚未配置可读取的本机素材目录。")
    roots = []
    for value in configured:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("本机素材目录配置无效。")
        candidate = Path(value).resolve()
        if not candidate.is_absolute() or candidate.parent == candidate or str(candidate).startswith("\\\\"):
            raise ValueError("本机素材目录配置必须是具体的本地绝对路径。")
        roots.append(candidate)
    return tuple(roots)


def scan_batch_folder(
    folder_text: str, allowed_roots: tuple[Path, ...] | None = None
) -> tuple[Path, list[dict[str, Any]]]:
    if not isinstance(folder_text, str) or not folder_text.strip():
        raise ValueError("请输入要读取的本机文件夹路径。")
    cleaned = folder_text.strip()
    if len(cleaned) > 1024:
        raise ValueError("文件夹路径过长。")
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == '"':
        cleaned = cleaned[1:-1].strip()
    if cleaned.startswith("\\\\"):
        raise ValueError("批量读取只支持本机磁盘，不读取网络共享路径。")
    requested = Path(cleaned)
    if not requested.is_absolute():
        raise ValueError("请填写完整的绝对路径，例如 D:/素材/姿势。")
    try:
        root = requested.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise ValueError("找不到这个文件夹，请检查路径后重试。") from error
    if not root.is_dir():
        raise ValueError("填写的路径不是文件夹。")
    if root.parent == root:
        raise ValueError("不能直接读取整个磁盘根目录，请选择一个具体图片文件夹。")
    roots = batch_import_roots() if allowed_roots is None else tuple(path.resolve() for path in allowed_roots)
    if not any(root == allowed or root.is_relative_to(allowed) for allowed in roots):
        raise ValueError("这个文件夹不在工作台允许的本机素材目录中。请在 config.json 的 batch_import_roots 中添加素材目录。")

    candidates: list[dict[str, Any]] = []
    visited_entries = 0
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            directory_names[:] = []
            continue
        if depth >= BATCH_SCAN_MAX_DEPTH:
            directory_names[:] = []
        visited_entries += len(directory_names) + len(file_names)
        if visited_entries > BATCH_SCAN_MAX_ENTRIES:
            raise ValueError("文件夹项目过多，请只保留本次需要生成的图片后重试。")
        safe_directories = []
        for directory_name in directory_names:
            candidate = current_path / directory_name
            try:
                resolved = candidate.resolve(strict=True)
                is_junction = bool(getattr(candidate, "is_junction", lambda: False)())
                if not candidate.is_symlink() and not is_junction and resolved.is_relative_to(root):
                    safe_directories.append(directory_name)
            except (OSError, ValueError):
                continue
        directory_names[:] = safe_directories

        for file_name in file_names:
            candidate = current_path / file_name
            if candidate.suffix.lower() not in BATCH_IMAGE_SUFFIXES:
                continue
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
                relative_path = resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            candidates.append(
                {
                    "path": resolved,
                    "relative_path": relative_path,
                }
            )
            if len(candidates) > BATCH_SELECTION_MAX_FILES:
                raise ValueError(
                    f"一次最多处理 {BATCH_SELECTION_MAX_FILES} 张图片；这个文件夹更多，请拆成较小的文件夹。"
                )

    if not candidates:
        raise ValueError("文件夹中没有找到 PNG、JPEG 或 WebP 图片。")
    candidates.sort(key=lambda item: batch_natural_key(item["relative_path"]))
    for item in candidates:
        relative_path = item["relative_path"]
        try:
            with Path(item["path"]).open("rb") as source_handle:
                before = os.fstat(source_handle.fileno())
                if before.st_size <= 0:
                    raise ValueError(f"{relative_path} 是空文件。")
                if before.st_size > MAX_SOURCE_BYTES:
                    raise ValueError(f"{relative_path} 超过自动压缩的单张 200MB 输入上限。")
                payload = source_handle.read(MAX_SOURCE_BYTES + 1)
                after = os.fstat(source_handle.fileno())
            if len(payload) != before.st_size or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise ValueError(f"{relative_path} 正在被修改，请稍后重新读取文件夹。")
            prepared = prepare_photo(payload, BATCH_CACHE_ROOT)
            item.update(prepared)
            width, height = prepared["preparation"]["width"], prepared["preparation"]["height"]
        except Exception as error:
            for candidate in candidates:
                if candidate.get("prepared_path"):
                    remove_cached(candidate["prepared_path"], BATCH_CACHE_ROOT)
            raise ValueError(f"{relative_path}：{error}") from error
        item["size"] = before.st_size
        item["mtime_ns"] = before.st_mtime_ns
        item["device"] = before.st_dev
        item["inode"] = before.st_ino
        item["source_sha256"] = hashlib.sha256(payload).hexdigest()
        item["width"] = width
        item["height"] = height
    return root, candidates


def discard_batch_selection(selection: dict[str, Any]) -> None:
    for item in selection.get("files", {}).values():
        if item.get("prepared_path"):
            remove_cached(item["prepared_path"], BATCH_CACHE_ROOT)


async def batch_import_thread(function, *args, **kwargs):
    result = None
    def work():
        nonlocal result
        result = function(*args, **kwargs)
        return result
    try:
        return await joined_thread(work)
    except BaseException:
        items = result[1] if isinstance(result, tuple) else [result] if isinstance(result, dict) else []
        for item in items:
            if item.get("prepared_path"):
                remove_cached(item["prepared_path"], BATCH_CACHE_ROOT)
        raise


def prune_batch_selections(registry: dict[str, dict[str, Any]], now: float | None = None) -> None:
    current_time = time.time() if now is None else now
    expired = [
        selection_id
        for selection_id, selection in registry.items()
        if current_time - float(selection.get("last_used_at", 0)) > BATCH_SELECTION_TTL_SECONDS
    ]
    for selection_id in expired:
        discard_batch_selection(registry.pop(selection_id))


def public_batch_file(file_id: str, item: dict[str, Any]) -> dict[str, Any]:
    preparation = item.get("preparation", {})
    return {"id": file_id, "relative_path": item["relative_path"],
            "size": preparation.get("size", item["size"]), "width": item["width"], "height": item["height"],
            "preparation": preparation}


def register_batch_selection(
    registry: dict[str, dict[str, Any]], root: Path, files: list[dict[str, Any]], append_to: str | None = None
) -> dict[str, Any]:
    now = time.time()
    prune_batch_selections(registry, now)
    if append_to:
        existing = registry.get(append_to)
        if not existing or existing["root"] != BATCH_CACHE_ROOT.resolve() or root != BATCH_CACHE_ROOT.resolve():
            raise ValueError("批量上传记录已失效，请重新选择照片。")
        stored_files = existing["files"]
        if len(stored_files) + len(files) > BATCH_SELECTION_MAX_FILES:
            raise ValueError("一次最多导入 200 张照片。")
        selection_id = append_to
    else:
        while len(registry) >= BATCH_SELECTION_MAX_REGISTRIES:
            oldest = min(registry, key=lambda key: float(registry[key].get("last_used_at", 0)))
            discard_batch_selection(registry.pop(oldest))
        selection_id = secrets.token_urlsafe(32)
        stored_files = {}
    public_files = []
    for item in files:
        file_id = secrets.token_urlsafe(32)
        stored_files[file_id] = dict(item)
        stored_files[file_id]["claimed"] = False
        public_files.append(public_batch_file(file_id, item))
    registry[selection_id] = {
        "root": root,
        "files": stored_files,
        "created_at": now,
        "last_used_at": now,
    }
    return {
        "selection_id": selection_id,
        "folder_name": root.name,
        "file_count": len(public_files),
        "files": public_files,
    }


def read_batch_selection_file(
    registry: dict[str, dict[str, Any]], selection_id: str, file_id: str
) -> tuple[bytes, str, str]:
    token_pattern = r"[A-Za-z0-9_-]{40,64}"
    if not re.fullmatch(token_pattern, selection_id or "") or not re.fullmatch(token_pattern, file_id or ""):
        raise ValueError("批量文件凭据无效，请重新读取文件夹。")
    prune_batch_selections(registry)
    selection = registry.get(selection_id)
    if not selection:
        raise ValueError("批量文件夹记录已失效，请重新读取。")
    item = selection["files"].get(file_id)
    if not item:
        raise ValueError("批量图片记录不存在，请重新读取文件夹。")
    if item.get("claimed"):
        raise ValueError("这张批量图片已经提交过；为避免重复生成，请重新读取文件夹。")
    item["claimed"] = True
    try:
        root = Path(selection["root"]).resolve(strict=True)
        source = Path(item["path"]).resolve(strict=True)
        if not source.is_relative_to(root) or not source.is_file() or source.is_symlink():
            raise ValueError("批量图片已移动或不再位于原文件夹中，请重新读取。")
        with source.open("rb") as source_handle:
            before = os.fstat(source_handle.fileno())
            if (
                before.st_size != item["size"]
                or before.st_mtime_ns != item["mtime_ns"]
                or before.st_dev != item["device"]
                or before.st_ino != item["inode"]
            ):
                raise ValueError(f"{item['relative_path']} 在读取后发生了变化，请重新读取文件夹。")
            payload = source_handle.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(source_handle.fileno())
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise ValueError(f"{item['relative_path']} 正在被修改，请稍后重新读取文件夹。")
    except ValueError:
        raise
    except OSError as error:
        raise ValueError(f"无法读取 {item['relative_path']}，请检查文件后重新读取文件夹。") from error
    if len(payload) <= 0 or len(payload) > MAX_SOURCE_BYTES:
        raise ValueError(f"{item['relative_path']} 的文件大小不再有效，请重新读取文件夹。")
    if hashlib.sha256(payload).hexdigest() != item["source_sha256"]:
        raise ValueError(f"{item['relative_path']} 的内容发生了变化，请重新读取文件夹。")
    suffix = source.suffix.lower()
    if suffix not in BATCH_IMAGE_SUFFIXES:
        raise ValueError("批量图片扩展名已发生变化，请重新读取文件夹。")
    if item.get("prepared_path"):
        prepared = Path(item["prepared_path"])
        if prepared.parent.resolve() != BATCH_CACHE_ROOT.resolve() or prepared.is_symlink():
            raise ValueError("照片兼容副本路径无效，请重新导入。")
        try:
            with prepared.open("rb") as handle:
                payload = handle.read(25 * 1024 * 1024 + 1)
        except OSError as error:
            raise ValueError("照片兼容副本已失效，请重新导入。") from error
        if len(payload) > 25 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != item["prepared_sha256"]:
            raise ValueError("照片兼容副本发生变化，请重新导入。")
        suffix = prepared.suffix.lower()
        remove_cached(prepared, BATCH_CACHE_ROOT)
    selection["last_used_at"] = time.time()
    return payload, suffix, item["relative_path"]


def prepare_image(payload: bytes, suffix: str) -> tuple[Path, tuple[int, int], str]:
    # All photo entry points share the same full-frame compression policy.
    # Run it before Pillow opens the source, including for pixel-only overflow.
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    compatible = None
    final_compatible = None
    source = UPLOAD_ROOT / f"incoming-{uuid.uuid4().hex}{suffix}"
    output = UPLOAD_ROOT / f"h3-{uuid.uuid4().hex}.png"
    try:
        prepared = prepare_photo(payload, BATCH_CACHE_ROOT)
        compatible = prepared.get("prepared_path")
        if compatible:
            payload = Path(compatible).read_bytes()
        source.write_bytes(payload)
        with Image.open(source) as opened:
            if opened.width * opened.height > 40_000_000:
                raise ValueError("图片像素超过 4000 万，请先缩小后再上传。")
            # Phone HDR / multi-picture JPEG exports can be identified as MPO.
            # Frame zero is the main photo; auxiliary frames are not references.
            if opened.format not in {"PNG", "JPEG", "WEBP", "MPO"}:
                raise ValueError("图片真实格式不受支持，请使用 PNG、JPEG 或 WebP。")
            opened.verify()
        with Image.open(source) as opened:
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            image.save(output, format="PNG", optimize=True)
            size = (image.width, image.height)
        # A compact camera JPEG can become oversized when normalized to PNG.
        # Check the final artifact too, preserving the whole frame and alpha.
        if output.stat().st_size > MAX_IMPORT_BYTES:
            final = prepare_photo(output, BATCH_CACHE_ROOT)
            final_compatible = final["prepared_path"]
            replacement = output.with_suffix(Path(final_compatible).suffix)
            try:
                shutil.copy2(final_compatible, replacement)
            except BaseException:
                if replacement != output:
                    replacement.unlink(missing_ok=True)
                raise
            if replacement != output:
                output.unlink(missing_ok=True)
            output = replacement
            size = (final["preparation"]["width"], final["preparation"]["height"])
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return output, size, digest
    except Image.DecompressionBombError as error:
        output.unlink(missing_ok=True)
        raise ValueError("图片像素过大，请先缩小后重试。") from error
    except (OSError, SyntaxError) as error:
        output.unlink(missing_ok=True)
        raise ValueError("图片无法解码，请重新导出为 PNG、JPEG 或 WebP。") from error
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        source.unlink(missing_ok=True)
        if compatible:
            remove_cached(compatible, BATCH_CACHE_ROOT)
        if final_compatible:
            remove_cached(final_compatible, BATCH_CACHE_ROOT)


def prepare_image_file(source: Path) -> tuple[Path, tuple[int, int], str]:
    """Normalize a disk-backed upload; never modify the original or run a model."""
    prepared = prepare_photo(source, BATCH_CACHE_ROOT, keep_compatible=True)
    compatible = Path(prepared["prepared_path"])
    try:
        return prepare_image(compatible.read_bytes(), compatible.suffix)
    finally:
        remove_cached(compatible, BATCH_CACHE_ROOT)


def cleanup_cutout_outputs(outputs: list[Path]) -> None:
    for output in outputs:
        output.unlink(missing_ok=True)
        output.with_name(f"{output.name}.part").unlink(missing_ok=True)


async def stop_cutout_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def preprocess_portrait_references(
    reference_slots: list[Path | None],
    cancel_event: asyncio.Event | None = None,
    process_owner: dict[str, Any] | None = None,
) -> tuple[list[Path | None], dict[str, Any], list[Path]]:
    """Run the fixed local BiRefNet worker for every supplied reference slot."""

    if len(reference_slots) != 3 or reference_slots[0] is None:
        raise ValueError("抠图预处理需要第 1 张人物母版，并保持三个固定参考槽。")
    state = image_model_state()["cutout"]
    if not state["ready"]:
        raise ValueError("本地 BiRefNet 抠图模型或 Python 运行环境尚未准备完整。")
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("本地人像抠图已停止。")

    selected = [(index, path) for index, path in enumerate(reference_slots) if path is not None]
    outputs = [UPLOAD_ROOT / f"cutout-{uuid.uuid4().hex}.png" for _ in selected]
    command = [
        str(CUTOUT_PYTHON),
        str(CUTOUT_WORKER),
        "--model",
        str(CUTOUT_MODEL),
    ]
    for (_, source), output in zip(selected, outputs, strict=True):
        command.extend(("--input", str(source), "--output", str(output)))
    process: asyncio.subprocess.Process | None = None
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
    cancel_task: asyncio.Task[bool] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(APP_ROOT),
            env=_isolated_python_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process_owner is not None:
            process_owner["_cutout_process"] = process
        communicate_task = asyncio.create_task(process.communicate())
        waiters: set[asyncio.Task[Any]] = {communicate_task}
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            waiters.add(cancel_task)
        done, _ = await asyncio.wait(waiters, timeout=900, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            await stop_cutout_process(process)
            raise TimeoutError("本地人像抠图在 900 秒内没有完成。")
        if cancel_task is not None and cancel_task in done and cancel_event.is_set():
            await stop_cutout_process(process)
            await asyncio.gather(communicate_task, return_exceptions=True)
            raise RuntimeError("本地人像抠图已停止。")
        stdout_bytes, stderr_bytes = await communicate_task
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = (stderr or stdout or "未知错误").strip()[-3000:]
            raise ValueError(f"本地人像抠图失败：{detail}")
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise ValueError("本地人像抠图没有返回校验结果。")
        payload = json.loads(lines[-1])
        if not isinstance(payload, dict):
            raise ValueError("本地人像抠图返回的数据结构不正确。")
        if str(payload.get("model_sha256", "")).lower() != CUTOUT_MODEL_SHA256:
            raise ValueError("BiRefNet 抠图模型哈希与已登记版本不一致。")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(selected):
            raise ValueError("本地人像抠图返回的结果数量不正确。")
        if any(not isinstance(result, dict) for result in results):
            raise ValueError("本地人像抠图返回的结果项结构不正确。")

        processed_slots = list(reference_slots)
        public_results: list[dict[str, Any]] = []
        for (index, _), output, result in zip(selected, outputs, results, strict=True):
            if not output.is_file() or output.stat().st_size <= 0:
                raise ValueError(f"第 {index + 1} 张参考图的抠图结果没有落盘。")
            with Image.open(output) as opened:
                opened.verify()
                if opened.mode != "RGBA":
                    raise ValueError(f"第 {index + 1} 张参考图的抠图结果没有透明通道。")
            output_digest = hashlib.sha256(output.read_bytes()).hexdigest()
            if output_digest != str(result.get("output_sha256", "")).lower():
                raise ValueError(f"第 {index + 1} 张参考图的抠图结果哈希校验失败。")
            processed_slots[index] = output
            public_results.append(
                {
                    "slot": REFERENCE_SLOT_NAMES[index],
                    "width": result.get("width"),
                    "height": result.get("height"),
                    "foreground_coverage": result.get("foreground_coverage"),
                    "foreground_box": result.get("foreground_box"),
                    "output_sha256": output_digest,
                    "warnings": result.get("warnings") or [],
                }
            )
        metadata = {
            "mode": "person_cutout",
            "model": payload.get("model"),
            "model_sha256": payload.get("model_sha256"),
            "provider": payload.get("provider"),
            "neutral_background_rgb": [235, 235, 235],
            "alpha_policy": "rgba_preserved_rgb_precomposited_for_comfy_loadimage",
            "processed_slots": [REFERENCE_SLOT_NAMES[index] for index, _ in selected],
            "fallback_used": False,
            "results": public_results,
        }
        return processed_slots, metadata, outputs
    except asyncio.CancelledError:
        await stop_cutout_process(process)
        if communicate_task is not None:
            await asyncio.gather(communicate_task, return_exceptions=True)
        cleanup_cutout_outputs(outputs)
        raise
    except Exception:
        try:
            await stop_cutout_process(process)
        finally:
            cleanup_cutout_outputs(outputs)
        raise
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        if process_owner is not None and process_owner.get("_cutout_process") is process:
            process_owner.pop("_cutout_process", None)


def render_workflow(
    mode: str,
    prompt: str,
    seed: int,
    quality: str,
    aspect: str,
    uploaded_name: str | None,
    canvas: tuple[int, int] | None = None,
    steps: int | None = None,
    identity_name: str | None = None,
) -> tuple[dict[str, Any], int, int]:
    preset = MANIFEST["presets"][quality]
    width, height = canvas or tuple(preset[aspect])
    template_name = MANIFEST["templates"][mode]
    graph = copy.deepcopy(json.loads((WORKFLOW_ROOT / template_name).read_text(encoding="utf-8")))
    graph["5"]["inputs"].update({"prompt": prompt, "width": width, "height": height, "length": VIDEO_FRAMES})
    graph["6"]["inputs"]["noise_seed"] = seed
    graph["8"]["inputs"]["steps"] = preset["steps"] if steps is None else steps
    if mode == "continue_dual":
        from dual_reference import apply_dual_reference
        if quality not in {"standard", "native"} or graph["8"]["inputs"]["steps"] < 20:
            raise ValueError("双参考实验只支持原版标准/高清档、至少 20 步。")
        apply_dual_reference(graph, identity_name)
    if preset.get("encoder") == "clipproj_4b":
        graph["2"]["inputs"].update(
            clip_name="qwen3vl_4b_fp8_scaled.safetensors", type="krea2", device="default"
        )
        graph["21"] = {
            "class_type": "ClipProjApply",
            "inputs": {"clip": ["2", 0], "projection": "mmh3-4b-ClipProj-v3.1.safetensors"},
        }
        graph["5"]["inputs"]["clip"] = ["21", 0]
    if preset.get("turbo", False):
        graph["17"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["1", 0],
                "lora_name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
                "strength_model": 1.0,
            },
        }
        graph["8"]["inputs"]["model"] = ["17", 0]
        graph["9"]["inputs"]["model"] = ["17", 0]
    if mode == "image":
        if not uploaded_name:
            raise ValueError("图生视频缺少上传图片。")
        graph["15"]["inputs"]["image"] = uploaded_name
        graph["16"]["inputs"].update({"width": width, "height": height, "crop": "center"})
    elif mode in {"continue", "continue_dual"}:
        if not uploaded_name:
            raise ValueError("接着生成缺少上一段视频。")
        graph["18"]["inputs"]["file"] = uploaded_name
    return graph, width, height


async def upload_to_comfy(
    session: ClientSession,
    input_path: Path,
    content_type: str = "image/png",
) -> tuple[str, dict[str, Any]]:
    with input_path.open("rb") as input_file:
        form = FormData()
        form.add_field("image", input_file, filename=input_path.name, content_type=content_type)
        form.add_field("type", "input")
        form.add_field("overwrite", "false")
        async with session.post(
            f"{COMFY_URL}/upload/image", data=form, timeout=ClientTimeout(total=300)
        ) as response:
            payload = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"上传素材到本地 ComfyUI 失败：{payload}")
    name = payload["name"]
    subfolder = payload.get("subfolder", "")
    stored_name = f"{subfolder}/{name}" if subfolder else name
    return stored_name.replace("\\", "/"), payload


def stage_for_node(node_id: str | None) -> str:
    stages = {
        "1": "加载视频模型",
        "2": "理解提示词",
        "3": "加载画面解码器",
        "4": "加载声音解码器",
        "5": "准备画面与声音",
        "10": "生成视频",
        "11": "解码画面",
        "12": "解码声音",
        "13": "合成视频",
        "14": "保存视频",
        "15": "读取上传图片",
        "16": "适配图片画幅",
        "18": "读取上一段结尾",
        "19": "拆分连续画面与声音",
        "20": "锚定连续画面与声音",
        "21": "应用 ClipProj 小型编码器",
        "22": "读取固定角色参考图",
    }
    return stages.get(str(node_id), "本地生成中")


async def wait_for_execution(app: web.Application, job: dict[str, Any], client_id: str, graph: dict[str, Any]) -> None:
    session: ClientSession = app["session"]
    prompt_id = job["id"]
    ws_url = COMFY_URL.replace("http://", "ws://").replace("https://", "wss://")
    submitted = False
    try:
        async with asyncio.timeout(4 * 60 * 60):
            async with session.ws_connect(f"{ws_url}/ws?clientId={client_id}", heartbeat=30) as socket:
                request_payload = {
                    "prompt": graph,
                    "client_id": client_id,
                    "prompt_id": prompt_id,
                    "extra_data": {"workflow_id": job["workflow"], "source": "h3-local-studio"},
                }
                async with session.post(
                    f"{COMFY_URL}/prompt", json=request_payload, timeout=ClientTimeout(total=90)
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status != 200:
                        raise RuntimeError(f"工作流预检失败：{json.dumps(result, ensure_ascii=False)}")
                submitted = True
                job.update(status="queued", stage="已进入本地生成队列")
                if job["cancel_event"].is_set():
                    await cancel_remote_job(session, prompt_id)
                    job.update(status="cancelled", stage="已停止")
                    return

                while True:
                    receive_task = asyncio.create_task(socket.receive())
                    cancel_task = asyncio.create_task(job["cancel_event"].wait())
                    done, pending = await asyncio.wait(
                        {receive_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if cancel_task in done and cancel_task.result():
                        receive_task.cancel()
                        await asyncio.gather(receive_task, return_exceptions=True)
                        await cancel_remote_job(session, prompt_id)
                        job.update(status="cancelled", stage="已停止")
                        return
                    message = receive_task.result()
                    if message.type != WSMsgType.TEXT:
                        if message.type in {WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR}:
                            break
                        continue
                    event = json.loads(message.data)
                    data = event.get("data", {})
                    if data.get("prompt_id") not in {None, prompt_id}:
                        continue
                    event_type = event.get("type")
                    if event_type == "execution_start":
                        job.update(status="running", stage="开始生成")
                    elif event_type == "executing":
                        job.update(status="running", stage=stage_for_node(data.get("node")))
                    elif event_type == "progress":
                        job.update(
                            status="running",
                            stage="生成视频",
                            progress_value=int(data.get("value", 0)),
                            progress_max=int(data.get("max", 0)),
                        )
                    elif event_type == "execution_error":
                        raise RuntimeError(data.get("exception_message") or data.get("exception_type") or "ComfyUI 执行失败")
                    elif event_type == "execution_interrupted":
                        job.update(status="cancelled", stage="已停止")
                        return
                    elif event_type == "execution_success":
                        return
            await poll_remote_job(session, job)
    except TimeoutError as error:
        if submitted:
            await cancel_remote_job(session, prompt_id)
        raise RuntimeError("生成超过 4 小时，已请求停止该任务。") from error
    except (ClientError, OSError) as error:
        if not submitted:
            raise RuntimeError(f"无法连接本地 ComfyUI：{error}") from error
        await poll_remote_job(session, job)


async def cancel_remote_job(session: ClientSession, prompt_id: str) -> bool:
    try:
        async with session.post(
            f"{COMFY_URL}/api/jobs/{prompt_id}/cancel", timeout=ClientTimeout(total=20)
        ) as response:
            payload = await response.json(content_type=None)
            return response.status == 200 and bool(payload.get("cancelled"))
    except (ClientError, OSError, asyncio.TimeoutError):
        return False


async def poll_remote_job(session: ClientSession, job: dict[str, Any]) -> None:
    prompt_id = job["id"]
    while True:
        if job["cancel_event"].is_set():
            await cancel_remote_job(session, prompt_id)
            job.update(status="cancelled", stage="已停止")
            return
        try:
            async with session.get(f"{COMFY_URL}/api/jobs/{prompt_id}", timeout=ClientTimeout(total=20)) as response:
                if response.status == 404:
                    await asyncio.sleep(2)
                    continue
                payload = await response.json(content_type=None)
                if response.status != 200:
                    raise RuntimeError(f"读取生成状态失败：HTTP {response.status}")
        except (ClientError, OSError, asyncio.TimeoutError):
            job.update(stage="连接暂时中断，正在恢复")
            await asyncio.sleep(3)
            continue
        status = payload.get("status")
        if status == "pending":
            job.update(status="queued", stage="等待本地生成")
        elif status == "in_progress":
            job.update(status="running", stage="本地生成中")
        elif status == "completed":
            return
        elif status == "failed":
            details = payload.get("execution_error") or {}
            message = details.get("exception_message") if isinstance(details, dict) else str(details)
            raise RuntimeError(message or "ComfyUI 执行失败")
        await asyncio.sleep(2)


async def fetch_history(session: ClientSession, prompt_id: str) -> dict[str, Any]:
    for _ in range(30):
        async with session.get(
            f"{COMFY_URL}/history/{prompt_id}", timeout=ClientTimeout(total=20)
        ) as response:
            payload = await response.json(content_type=None)
        if prompt_id in payload:
            return payload[prompt_id]
        await asyncio.sleep(1)
    raise RuntimeError("生成已结束，但没有找到输出记录。")


def find_video_asset(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        filename = value.get("filename")
        if isinstance(filename, str) and Path(filename).suffix.lower() in VIDEO_SUFFIXES:
            return value
        for child in value.values():
            found = find_video_asset(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_video_asset(child)
            if found:
                return found
    return None


def safe_result_stem(value: str, fallback: str = "H3") -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    characters = []
    for character in normalized:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            continue
        characters.append("_" if character in '<>:"/\\|?*' else character)
    cleaned = re.sub(r"\s+", "_", "".join(characters))
    cleaned = re.sub(r"_+", "_", cleaned).strip(" ._-")
    cleaned = cleaned[:64].rstrip(" ._-")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return cleaned


def video_result_path(
    job: dict[str, Any], asset_filename: str, result_root: Path | None = None
) -> Path:
    suffix = Path(str(asset_filename)).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        raise RuntimeError("本地生成引擎返回的不是受支持的视频格式。")
    try:
        job_token = uuid.UUID(str(job["id"])).hex
    except (KeyError, ValueError, AttributeError) as error:
        raise RuntimeError("任务编号无效，无法安全保存结果。") from error
    batch_relative_path = job.get("batch_relative_path")
    if batch_relative_path:
        readable_stem = safe_result_stem(PurePosixPath(str(batch_relative_path)).stem)
        filename = f"{readable_stem}__H3__{job_token}{suffix}"
    elif job.get("continuation"):
        filename = f"H3_continue__{job_token}{suffix}"
    else:
        filename = f"H3__{job_token}{suffix}"
    root = video_result_directory(job, result_root)
    root.mkdir(parents=True, exist_ok=True)
    target = (root / filename).resolve()
    if target.parent != root:
        raise RuntimeError("结果文件名未通过本机路径校验。")
    if target.exists():
        raise RuntimeError("同名结果已经存在，工作台拒绝覆盖原文件。")
    return target


def video_result_directory(job: dict[str, Any], result_root: Path | None = None) -> Path:
    root = (RESULT_ROOT if result_root is None else Path(result_root)).resolve()
    relative = job.get("output_subdir")
    if not relative:
        return root
    parts = PurePosixPath(str(relative)).parts
    if parts and parts[0] == "剧本连续":
        try:
            sequence_id = uuid.UUID(str(job.get("sequence_id") or job["id"])).hex
        except (KeyError, ValueError, AttributeError) as error:
            raise ValueError("剧本结果编号无效。") from error
        if (len(parts) != 2 or "\\" in str(relative) or parts[1] != safe_result_stem(parts[1])
                or not parts[1].endswith(sequence_id)):
            raise ValueError("剧本结果目录无效。")
        directory = (root / Path(*parts)).resolve()
        if not directory.is_relative_to(root) or directory == root:
            raise ValueError("剧本结果目录超出输出文件夹。")
        return directory
    try:
        batch_id = uuid.UUID(str(job["sequence_batch_id"])).hex
        uuid.UUID(str(job.get("sequence_id") or job["id"]))
    except (KeyError, ValueError, AttributeError) as error:
        raise ValueError("剧本批次结果编号无效。") from error
    if (len(parts) != 3 or parts[0] != "剧本批量" or "\\" in str(relative)
            or any(part != safe_result_stem(part) for part in parts[1:])
            or not parts[1].endswith(batch_id)):
        raise ValueError("剧本批次结果目录无效。")
    directory = (root / Path(*parts)).resolve()
    if not directory.is_relative_to(root) or directory == root:
        raise ValueError("剧本批次结果目录超出输出文件夹。")
    return directory


def video_result_url(job: dict[str, Any], filename: str) -> str:
    if job.get("output_subdir"):
        if not job.get("sequence_batch_id"):
            return f"/api/sequences/{job['sequence_id']}/videos/{quote(filename)}"
        return (f"/api/sequence-batches/{job['sequence_batch_id']}/variants/"
                f"{job['sequence_id']}/videos/{quote(filename)}")
    return f"/results/{quote(filename)}"


def result_metadata_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.metadata.json")


def write_result_metadata(output: Path, metadata: dict[str, Any]) -> Path:
    target = result_metadata_path(output)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
    try:
        if target.exists():
            raise RuntimeError("同名结果元数据已经存在，工作台拒绝覆盖原文件。")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.rename(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


async def download_result(session: ClientSession, job: dict[str, Any], asset: dict[str, Any]) -> Path:
    target = video_result_path(job, str(asset.get("filename", "")))
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
    params = {
        "filename": asset["filename"],
        "subfolder": asset.get("subfolder", ""),
        "type": asset.get("type", "output"),
    }
    try:
        async with session.get(
            f"{COMFY_URL}/view", params=params, timeout=ClientTimeout(total=600)
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"读取生成视频失败，HTTP {response.status}")
            with temporary.open("xb") as output:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    output.write(chunk)
        if target.exists():
            raise RuntimeError("同名结果已经存在，工作台拒绝覆盖原文件。")
        temporary.rename(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def probe_video(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"verified": False, "reason": "ffprobe 不在 PATH"}
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        return {"verified": False, "reason": result.stderr.strip()}
    payload = json.loads(result.stdout)
    stream_types = {stream.get("codec_type") for stream in payload.get("streams", [])}
    payload["verified"] = "video" in stream_types
    payload["has_audio"] = "audio" in stream_types
    return payload


def probe_continuation_source(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("接着生成需要 FFprobe，但当前系统没有找到它。")
    command = [
        ffprobe,
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,r_frame_rate,nb_frames,nb_read_frames",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"无法读取上一段视频：{result.stderr.strip()}")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("上一段结果中没有视频流，不能接着生成。")
    try:
        frame_rate = float(Fraction(video.get("r_frame_rate", "0/1")))
        frame_count = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    except (ValueError, ZeroDivisionError) as error:
        raise RuntimeError("无法确认上一段视频的帧率或帧数。") from error
    if abs(frame_rate - VIDEO_FPS) > 0.001:
        raise RuntimeError(f"上一段视频不是 {VIDEO_FPS}fps，不能可靠提取连续帧。")
    if frame_count < CONTINUATION_GUIDE_FRAMES:
        raise RuntimeError(f"上一段视频少于 {CONTINUATION_GUIDE_FRAMES} 帧，不能接着生成。")
    return {
        "frame_count": frame_count,
        "frame_rate": frame_rate,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def continuation_ffmpeg_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    frame_count: int,
    has_audio: bool,
) -> list[str]:
    start_frame = frame_count - CONTINUATION_GUIDE_FRAMES
    start_seconds = start_frame / VIDEO_FPS
    duration = CONTINUATION_GUIDE_FRAMES / VIDEO_FPS
    video_filter = (
        f"[0:v:0]trim=start_frame={start_frame}:end_frame={frame_count},"
        "setpts=PTS-STARTPTS[v]"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if has_audio:
        audio_filter = (
            f"[0:a:0]atrim=start={start_seconds:.9f}:duration={duration:.9f},"
            "asetpts=PTS-STARTPTS[a]"
        )
        command.extend(["-filter_complex", f"{video_filter};{audio_filter}", "-map", "[v]", "-map", "[a]"])
    else:
        command.extend(["-filter_complex", video_filter, "-map", "[v]"])
    command.extend(
        [
            "-frames:v",
            str(CONTINUATION_GUIDE_FRAMES),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if has_audio:
        command.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "32000", "-ac", "2"])
    command.extend(["-movflags", "+faststart", str(target)])
    return command


def prepare_continuation_clip(source: Path, target: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("接着生成需要 FFmpeg，但当前系统没有找到它。")
    source_info = probe_continuation_source(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = continuation_ffmpeg_command(
        ffmpeg,
        source,
        target,
        source_info["frame_count"],
        source_info["has_audio"],
    )
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"提取上一段末尾画面失败：{result.stderr.strip()}")
    tail_info = probe_continuation_source(target)
    if tail_info["frame_count"] != CONTINUATION_GUIDE_FRAMES:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"连续片段应为 {CONTINUATION_GUIDE_FRAMES} 帧，实际得到 {tail_info['frame_count']} 帧。"
        )
    if source_info["has_audio"] and not tail_info["has_audio"]:
        target.unlink(missing_ok=True)
        raise RuntimeError("上一段有声音，但连续片段没有保留下声音。")
    return {
        "path": target,
        "guide_frames": CONTINUATION_GUIDE_FRAMES,
        "guide_seconds": CONTINUATION_GUIDE_FRAMES / VIDEO_FPS,
        "has_audio": tail_info["has_audio"],
    }


def remove_comfy_input(upload: dict[str, Any] | None) -> None:
    if not upload:
        return
    input_root = (COMFY_ROOT / "input").resolve()
    candidate = (input_root / upload.get("subfolder", "") / Path(upload["name"]).name).resolve()
    if candidate == input_root or input_root not in candidate.parents:
        return
    candidate.unlink(missing_ok=True)


async def run_job(app: web.Application, job: dict[str, Any]) -> None:
    uploaded_to_comfy = None
    identity_uploaded_to_comfy = None
    local_image = Path(job["local_image"]) if job.get("local_image") else None
    continuation_source = Path(job["continuation_source"]) if job.get("continuation_source") else None
    continuation_clip = None
    continuation_clip_info = None
    output: Path | None = None
    try:
        async with app["gpu_lock"]:
            if job.get("cancel_requested"):
                job.update(status="cancelled", stage="已停止")
                return
            session: ClientSession = app["session"]
            identity_name = None
            if job["mode"] == "continue_dual":
                from dual_reference import verify_model
                await joined_thread(verify_model, COMFY_ROOT)
                identity_path = Path(job.get("identity_image") or "")
                if not identity_path.is_file():
                    raise ValueError("双参考续接缺少已保存的原始角色图。")
                job["identity_image_sha256"] = hashlib.sha256(identity_path.read_bytes()).hexdigest()
                identity_name, identity_uploaded_to_comfy = await upload_to_comfy(session, identity_path)
            # The 12GB GPU can host only one of H3, Qwen-Image, or the prompt
            # LLM at a time.  Image tasks always free their own weights, and a
            # best-effort free here also covers an externally started 8188.
            await free_comfy_models(session, IMAGE_COMFY_URL)
            uploaded_name = None
            if local_image:
                job.update(status="uploading", stage="把图片交给本地生成引擎")
                uploaded_name, uploaded_to_comfy = await upload_to_comfy(session, local_image)
            elif continuation_source:
                job.update(status="uploading", stage=f"提取上一段末尾 {CONTINUATION_GUIDE_FRAMES} 帧与声音")
                continuation_clip = UPLOAD_ROOT / f"h3-continuation-{job['id']}.mp4"
                continuation_clip_info = await joined_thread(
                    prepare_continuation_clip,
                    continuation_source,
                    continuation_clip,
                )
                uploaded_name, uploaded_to_comfy = await upload_to_comfy(
                    session,
                    continuation_clip,
                    content_type="video/mp4",
                )
            if job["cancel_event"].is_set():
                job.update(status="cancelled", stage="已停止")
                return
            graph, width, height = render_workflow(
                job["mode"],
                job["prompt"],
                job["seed"],
                job["quality"],
                job["aspect"],
                uploaded_name,
                tuple(job["canvas"]) if job.get("canvas") else None,
                steps=job.get("steps"),
                identity_name=identity_name,
            )
            job.update(width=width, height=height)
            client_id = str(uuid.uuid4())
            await wait_for_execution(app, job, client_id, graph)
            if job["status"] == "cancelled":
                return
            job.update(status="saving", stage="检查并保存成片")
            history = await fetch_history(session, job["id"])
            asset = find_video_asset(history.get("outputs", {}))
            if not asset:
                raise RuntimeError("工作流完成，但输出记录里没有 MP4/WebM 视频。")
            output = await download_result(session, job, asset)
            media = await asyncio.to_thread(probe_video, output)
            if not media.get("verified"):
                raise RuntimeError(f"输出文件没有通过 FFprobe 视频流校验：{media.get('reason', 'unknown error')}")
            output_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
            metadata = {
                "result_layout": ("sequence-batch-v1" if job.get("sequence_batch_id") else "sequence-v1") if job.get("output_subdir") else "flat-v1",
                "prompt_id": job["id"],
                "workflow": job["workflow"],
                "prompt": job["prompt"],
                "character_lock": job.get("character_lock", ""),
                "seed": job["seed"],
                "quality": job["quality"],
                "steps": graph["8"]["inputs"]["steps"],
                "image_fit": job.get("image_fit"),
                "continuation_strategy": job.get("continuation_strategy", "previous"),
                "identity_image_sha256": job.get("identity_image_sha256"),
                "diffusion_model": graph["1"]["inputs"]["unet_name"],
                "conditioning_prompt": graph["5"]["inputs"]["prompt"],
                "encoder": MANIFEST["presets"][job["quality"]].get("encoder", "original_32b"),
                "encoder_model": graph["2"]["inputs"]["clip_name"],
                "projection_model": graph.get("21", {}).get("inputs", {}).get("projection"),
                "aspect": job["aspect"],
                "width": job["width"],
                "height": job["height"],
                "frames": VIDEO_FRAMES,
                "fps": VIDEO_FPS,
                "input_image_sha256": job.get("input_image_sha256"),
                "batch_relative_path": job.get("batch_relative_path"),
                "batch_image_preparation": job.get("batch_image_preparation"),
                "output": str(output),
                "output_sha256": output_sha256,
                "media": media,
                "completed_at": time.time(),
                "sequence_id": job.get("sequence_id"),
                "shot_index": job.get("shot_index"),
                "sequence_batch_id": job.get("sequence_batch_id"),
                "output_subdir": job.get("output_subdir"),
            }
            if continuation_source:
                metadata["continuation"] = {
                    "source_job_id": job["continue_from_job_id"],
                    "source_output_sha256": job.get("continuation_source_sha256"),
                    "guide_frames": CONTINUATION_GUIDE_FRAMES,
                    "guide_seconds": CONTINUATION_GUIDE_FRAMES / VIDEO_FPS,
                    "guide_has_audio": bool(continuation_clip_info and continuation_clip_info["has_audio"]),
                }
            write_result_metadata(output, metadata)
            job.update(
                status="completed",
                stage="生成完成",
                filename=output.name,
                video_url=video_result_url(job, output.name),
                output=str(output),
                output_sha256=output_sha256,
                media=media,
                completed_at=metadata["completed_at"],
            )
    except asyncio.CancelledError:
        job.update(status="cancelled", stage="已停止")
        raise
    except Exception as error:
        if job.get("cancel_requested"):
            job.update(status="cancelled", stage="已停止")
        else:
            job.update(status="failed", stage="生成失败", error=str(error))
    finally:
        if output is not None and job.get("status") != "completed":
            output.unlink(missing_ok=True)
            result_metadata_path(output).unlink(missing_ok=True)
        if local_image:
            local_image.unlink(missing_ok=True)
        if continuation_clip:
            continuation_clip.unlink(missing_ok=True)
        remove_comfy_input(uploaded_to_comfy)
        remove_comfy_input(identity_uploaded_to_comfy)


IMAGE_CANVASES = {
    "portrait": (512, 896),
    "landscape": (896, 512),
    "square": (640, 640),
}


async def cancel_remote_image_job(app: web.Application, prompt_id: str | None) -> bool:
    if not prompt_id:
        return False
    session: ClientSession = app["session"]
    matching = next((j for j in app["image_jobs"].values() if j.get("remote_prompt_id") == prompt_id), {})
    base_url = app["legacy_image_backend" if matching.get("image_engine") == "legacy" else "image_backend"].base_url
    cancelled = False
    try:
        async with session.post(
            f"{base_url}/api/jobs/{prompt_id}/cancel", timeout=ClientTimeout(total=20)
        ) as response:
            cancelled = response.status == 200
    except (ClientError, OSError, asyncio.TimeoutError):
        pass
    if cancelled:
        return True
    try:
        async with session.post(
            f"{base_url}/queue",
            json={"delete": [prompt_id]},
            timeout=ClientTimeout(total=20),
        ) as response:
            cancelled = response.status in {200, 204}
        async with session.post(
            f"{base_url}/interrupt",
            json={"prompt_id": prompt_id},
            timeout=ClientTimeout(total=20),
        ) as response:
            cancelled = response.status in {200, 204} or cancelled
    except (ClientError, OSError, asyncio.TimeoutError):
        pass
    return cancelled


def probe_generated_image(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as opened:
            opened.verify()
        with Image.open(path) as opened:
            return {"verified": True, "width": opened.width, "height": opened.height, "format": opened.format}
    except (OSError, ValueError) as error:
        return {"verified": False, "reason": str(error)}


def choose_image_generation_route(
    mode: str, reference_slots: list[Path | None]
) -> tuple[str, str]:
    """Derive the trusted backend route from semantic mode and uploaded slots."""

    if mode not in {"create", "edit"}:
        raise ValueError("未知的图片生成模式。")
    if len(reference_slots) != 3:
        raise ValueError("参考图必须保持三个固定槽位。")
    has_references = any(path is not None for path in reference_slots)
    if has_references and reference_slots[0] is None:
        raise ValueError("请先上传第 1 张主要人物参考，再添加第 2、3 张补充图。")
    if mode == "edit" and reference_slots[0] is None:
        raise ValueError("固定人物模式至少需要第 1 张角色母版。")
    if mode == "create" and not has_references:
        return "t2i", "text_master"
    return "edit", "create_master" if mode == "create" else "preserve_identity"


async def submit_image_generation(
    backend: ImageBackend,
    job: dict[str, Any],
    reference_slots: list[Path | None],
) -> dict[str, Any]:
    """Submit through one of the two reviewed image graphs only."""

    if job["generation_route"] == "t2i":
        if any(reference_slots):
            raise ValueError("纯文字母版任务不能携带参考图。")
        width, height = IMAGE_CANVASES[job["aspect"]]
        return await backend.submit_t2i(
            job["prompt"],
            negative_prompt=job["negative_prompt"],
            seed=job["seed"],
            width=width,
            height=height,
            steps=job["steps"],
        )
    if job["generation_route"] != "edit":
        raise ValueError("未知的图片工作流路由。")
    return await backend.submit_edit(
        job["prompt"],
        reference_slots,
        negative_prompt=job["negative_prompt"],
        seed=job["seed"],
        steps=job["steps"],
        intent=job["reference_intent"],
        **({"canvas": IMAGE_CANVASES[job["aspect"]]} if job.get("storyboard") else {}),
    )


async def run_image_job(app: web.Application, job: dict[str, Any]) -> None:
    backend: ImageBackend = app["legacy_image_backend" if job.get("image_engine") == "legacy" else "image_backend"]
    original_reference_slots = [
        Path(value) if value else None for value in job.get("local_reference_slots", [None, None, None])
    ]
    active_reference_slots = list(original_reference_slots)
    cutout_outputs: list[Path] = []
    submission: dict[str, Any] | None = None
    try:
        if job["generation_route"] == "edit" and job["reference_preprocess"]["mode"] == "person_cutout":
            job.update(status="starting", stage="在本机抠出参考图人物（CPU）")
            active_reference_slots, cutout_metadata, cutout_outputs = await preprocess_portrait_references(
                original_reference_slots, job["cancel_event"], job
            )
            job["reference_preprocess"] = cutout_metadata
            if job["cancel_event"].is_set():
                job.update(status="cancelled", stage="已停止")
                return
        async with app["gpu_lock"]:
            if job["cancel_event"].is_set():
                job.update(status="cancelled", stage="已停止")
                return
            job.update(status="starting", stage="释放视频模型显存")
            await free_comfy_models(app["session"], COMFY_URL)
            if job["cancel_event"].is_set():
                job.update(status="cancelled", stage="已停止")
                return

            job.update(status="starting", stage="启动并检查本地图片引擎")
            submission = await submit_image_generation(backend, job, active_reference_slots)
            job["remote_prompt_id"] = submission["prompt_id"]
            job["uploaded_references"] = submission.get("uploads", [])
            metadata = submission.get("metadata", {})
            repeated_slots = list(metadata.get("repeated_master_slots", []))
            job["reference_policy"] = {
                "supplied_count": metadata.get("supplied_reference_count", 0),
                "supplied_slots": metadata.get("supplied_reference_slots", []),
                "repeated_master_slots": repeated_slots,
                "duplicated_primary": bool(repeated_slots),
                "duplicated_master": bool(repeated_slots) and job["reference_intent"] == "preserve_identity",
                "reference_intent": job["reference_intent"],
            }
            job.update(status="queued", stage="图片任务已进入本地队列")

            while True:
                if job["cancel_event"].is_set():
                    await cancel_remote_image_job(app, job.get("remote_prompt_id"))
                    job.update(status="cancelled", stage="已停止")
                    return
                state = await backend.poll(submission["prompt_id"])
                if state["status"] == "completed":
                    break
                if state["status"] == "running":
                    job.update(status="running", stage="生成角色图片")
                else:
                    job.update(status="queued", stage="等待本地图片引擎")
                await asyncio.sleep(2)

            job.update(status="saving", stage="检查并保存图片")
            output_info = await backend.download_result(state["asset"], RESULT_ROOT / job["id"])
            output = Path(output_info["path"])
            media = await asyncio.to_thread(probe_generated_image, output)
            if not media.get("verified"):
                raise RuntimeError(f"输出图片没有通过解码校验：{media.get('reason', 'unknown error')}")
            job.update(width=media["width"], height=media["height"])
            completed_at = time.time()
            result_metadata = {
                "job_id": job["id"],
                "remote_prompt_id": submission["prompt_id"],
                "workflow": metadata.get("workflow_id"),
                "image_engine": job.get("image_engine", "qwen21"),
                "workflow_graph": metadata.get("graph"),
                "mode": job["mode"],
                "generation_route": job["generation_route"],
                "reference_intent": job["reference_intent"],
                "prompt": job["prompt"],
                "storyboard": job.get("storyboard"),
                "character_lock": job.get("character_lock", ""),
                "negative_prompt": job["negative_prompt"],
                "seed": job["seed"],
                "steps": job["steps"],
                "quality": job["quality"],
                "aspect": job["aspect"],
                "width": media["width"],
                "height": media["height"],
                "output": str(output),
                "output_sha256": output_info["sha256"],
                "reference_policy": job["reference_policy"],
                "reference_preprocess": job["reference_preprocess"],
                "source_reference_sha256": job.get("source_reference_sha256", {}),
                "normalized_reference_sha256": job.get("normalized_reference_sha256", {}),
                "batch_item_id": job.get("batch_item_id"),
                "batch_relative_path": job.get("batch_relative_path"),
                "batch_image_preparation": job.get("batch_image_preparation"),
                "input_reference_sha256": {
                    item.get("reference_slot", f"slot_{index + 1}"): item.get("source_sha256")
                    for index, item in enumerate(submission.get("uploads", []))
                },
                "runtime_validation": {
                    "workflow_declared": metadata.get("runtime_validation"),
                    "this_job": "prompt_completed_output_downloaded_sha256_and_decode_pass",
                },
                "completed_at": completed_at,
            }
            (output.parent / "metadata.json").write_text(
                json.dumps(result_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            job.update(
                status="completed",
                stage="图片生成完成",
                image_url=f"/results/{job['id']}/{quote(output.name)}",
                filename=output.name,
                output=str(output),
                output_sha256=output_info["sha256"],
                completed_at=completed_at,
            )
    except asyncio.CancelledError:
        await cancel_remote_image_job(app, job.get("remote_prompt_id"))
        job.update(status="cancelled", stage="已停止")
        raise
    except (ImageBackendError, RuntimeError, OSError, ValueError) as error:
        if job.get("cancel_requested"):
            job.update(status="cancelled", stage="已停止")
        else:
            message = error.message if isinstance(error, ImageBackendError) else str(error)
            job.update(status="failed", stage="图片生成失败", error=message)
    finally:
        if job.get("storyboard"):
            try:
                app["storyboards"].finish(job)
            except (OSError, ValueError) as error:
                job.update(error=f"图片任务结束，但分镜归档失败：{error}")
        if submission:
            backend.cleanup_uploads(submission.get("uploads", []))
        for reference in {path for path in original_reference_slots if path is not None}:
            reference.unlink(missing_ok=True)
        cleanup_cutout_outputs(cutout_outputs)
        await backend.free()


async def handle_optimize_prompt(request: web.Request) -> web.Response:
    async with request.app["submission_lock"]:
        current = active_gpu_job(request.app)
        if current or request.app["submission_state"]["reserved"]:
            return json_response({"error": "已有一个本地 AI 任务正在运行，请完成后再优化提示词。"}, status=409)
        request.app["submission_state"]["reserved"] = True
    try:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return json_response({"error": "请求不是有效的 JSON。"}, status=400)
        if not isinstance(payload, dict):
            return json_response({"error": "请求内容必须是对象。"}, status=400)
        if "model" in payload or "system_prompt" in payload:
            return json_response({"error": "浏览器不能直接指定模型或系统提示词。"}, status=400)
        mode = str(payload.get("mode", "video"))
        prompt_text = str(payload.get("prompt", "")).strip()
        character_lock = str(payload.get("character_lock", ""))
        try:
            if "model_profile" in payload:
                model_profile, profile = resolve_prompt_model_profile(payload["model_profile"])
            else:
                model_profile, profile = resolve_prompt_model_profile()
        except TypeError:
            return json_response({"error": "提示词模型模式必须是文字。"}, status=400)
        except ValueError:
            return json_response({"error": "未知的提示词模型模式。"}, status=400)
        if mode not in {"image", "video", "continuation", "storyboard"}:
            return json_response({"error": "未知的提示词类型。"}, status=400)
        if mode == "storyboard":
            try:
                project = request.app["storyboards"].read(payload.get("project_id", ""))
                shot = next(s for s in project["shots"] if s["id"] == payload.get("shot_id"))
                roles = {r["id"]: r for r in project["characters"]}
                if not shot["cast"] or not shot["action"].strip():
                    raise ValueError("请选择人物并填写这一幕的动作。")
                cast = [{"角色": chr(65+i), "名称": roles[rid]["name"],
                         "固定描述": roles[rid]["description"], "已有身份照片": bool(roles[rid]["asset"])}
                        for i, rid in enumerate(shot["cast"])]
                character_lock = json.dumps(cast, ensure_ascii=False)
                prompt_text = json.dumps({"原始动作": shot["action"], "角色表": cast,
                    "风格": project["style"], "画面方向": {"landscape": "横屏", "portrait": "竖屏", "square": "方形"}.get(payload.get("aspect"), "横屏"),
                    "有构图参考": bool(shot["guide"])}, ensure_ascii=False)
            except (ValueError, KeyError, TypeError, StopIteration):
                return json_response({"error": "请先保存有效的角色和分镜，再优化提示词。"}, status=400)
        if not prompt_text:
            return json_response({"error": "请先输入提示词。"}, status=400)
        if len(prompt_text) > (12000 if mode == "storyboard" else 5000) or len(character_lock) > (8000 if mode == "storyboard" else 2000):
            return json_response({"error": "提示词或人物固定描述过长。"}, status=400)
        manager: LocalPromptLLM = request.app["prompt_llm"]
        profile_state = prompt_install_state(manager)["profiles"][model_profile]
        if not profile_state["installed"]:
            return json_response({"error": f"{profile['label']}的本地模型尚未安装完整。"}, status=503)
        started = time.monotonic()
        async with request.app["gpu_lock"]:
            await free_comfy_models(request.app["session"], COMFY_URL)
            await request.app["image_backend"].free()
            if request.app.get("legacy_image_backend"):
                await request.app["legacy_image_backend"].free()
            try:
                optimization = await asyncio.to_thread(
                    manager.optimize,
                    mode,
                    prompt_text,
                    character_lock,
                    profile=profile["system_profile"],
                    timeout=240.0,
                )
            except (PromptLLMError, OSError, ValueError) as error:
                return json_response({"error": f"本地提示词优化失败：{error}"}, status=503)
        return json_response(
            {
                "optimization": optimization,
                "model": profile["model"],
                "model_profile": model_profile,
                "model_label": profile["label"],
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "model_unloaded": True,
            }
        )
    finally:
        async with request.app["submission_lock"]:
            request.app["submission_state"]["reserved"] = False


async def handle_generate_image(request: web.Request) -> web.Response:
    async with request.app["submission_lock"]:
        current = active_gpu_job(request.app)
        if current or request.app["submission_state"]["reserved"]:
            return json_response({"error": "已有一个本地 AI 任务正在运行，请等待或先停止。"}, status=409)
        request.app["submission_state"]["reserved"] = True
    local_reference_slots: list[Path | None] = [None, None, None]
    source_reference_sha256: dict[str, str] = {}
    normalized_reference_sha256: dict[str, str] = {}
    batch_item_id: str | None = None
    batch_relative_path: str | None = None
    batch_preparation: dict[str, Any] | None = None
    storyboard = None
    try:
        fields: dict[str, str] = {}
        reference_fields = {
            "reference_master": 0,
            "reference_pose": 1,
            "reference_detail": 2,
        }
        reader = await request.multipart()
        async for part in reader:
            if part.filename:
                if part.name not in reference_fields:
                    return json_response({"error": "未知的参考图上传槽位。"}, status=400)
                slot_index = reference_fields[part.name]
                if local_reference_slots[slot_index] is not None:
                    return json_response({"error": f"第 {slot_index + 1} 个参考图槽位重复上传。"}, status=400)
                suffix = Path(part.filename).suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                    return json_response({"error": "参考图只支持 PNG、JPEG、WebP。"}, status=400)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = await part.read_chunk(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        return json_response({"error": "单张参考图超过自动压缩的 200MB 输入上限。"}, status=400)
                    chunks.append(chunk)
                raw_payload = b"".join(chunks)
                source_digest = hashlib.sha256(raw_payload).hexdigest()
                async with request.app["batch_import_lock"]:
                    prepared, _, normalized_digest = await asyncio.to_thread(prepare_image, raw_payload, suffix)
                local_reference_slots[slot_index] = prepared
                source_reference_sha256[REFERENCE_SLOT_NAMES[slot_index]] = source_digest
                normalized_reference_sha256[REFERENCE_SLOT_NAMES[slot_index]] = normalized_digest
            else:
                fields[part.name] = await part.text()

        if fields.get("mode") == "storyboard":
            if any(local_reference_slots) or fields.get("batch_selection_id"):
                raise ValueError("分镜参考图只能来自已保存的角色库。")
            request_id = identifier(fields.get("storyboard_request_id", ""))
            storyboard = request.app["storyboards"].prepare(
                fields.get("storyboard_project_id", ""), fields.get("storyboard_shot_id", ""), fields.get("image_engine", "qwen21"))
            storyboard["export_dir"] = image_output.validate_folder(fields.get("export_dir", ""))
            fields["prompt"] = storyboard["prompt"]
            fields["character_lock"] = ""
            fields["reference_preprocess"] = "none"
            storyboard_paths = storyboard.pop("references")
            local_reference_slots = [None] * max(3, len(storyboard_paths))
            for slot, path in enumerate(storyboard_paths):
                temporary = UPLOAD_ROOT / f"{uuid.uuid4().hex}{path.suffix}"
                shutil.copy2(path, temporary)
                local_reference_slots[slot] = temporary
                digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                source_reference_sha256[f"image{slot+1}"] = digest
                normalized_reference_sha256[f"image{slot+1}"] = digest

        batch_selection_id = fields.get("batch_selection_id", "").strip()
        batch_file_id = fields.get("batch_file_id", "").strip()
        if bool(batch_selection_id) != bool(batch_file_id):
            return json_response({"error": "批量参考图凭据不完整，请重新导入。"}, status=400)
        if batch_selection_id and local_reference_slots[0] is not None:
            return json_response({"error": "不能同时上传第 1 张参考图并指定批量参考图。"}, status=400)
        if batch_selection_id:
            try:
                async with request.app["batch_registry_lock"]:
                    batch_payload, batch_suffix, batch_relative_path = await asyncio.to_thread(
                        read_batch_selection_file,
                        request.app["batch_selections"],
                        batch_selection_id,
                        batch_file_id,
                    )
                    batch_record = request.app["batch_selections"][batch_selection_id]["files"][batch_file_id]
                    batch_preparation = dict(batch_record.get("preparation", {}))
                    batch_source_digest = batch_record.get("source_sha256") or hashlib.sha256(batch_payload).hexdigest()
                async with request.app["batch_import_lock"]:
                    prepared, _, normalized_digest = await asyncio.to_thread(
                        prepare_image, batch_payload, batch_suffix
                    )
                local_reference_slots[0] = prepared
                source_reference_sha256[REFERENCE_SLOT_NAMES[0]] = batch_source_digest
                normalized_reference_sha256[REFERENCE_SLOT_NAMES[0]] = normalized_digest
                batch_item_id = batch_file_id
            except (KeyError, OSError, ValueError) as error:
                return json_response({"error": str(error)}, status=400)

        mode = fields.get("mode", "create")
        try:
            if storyboard:
                generation_route, reference_intent = "edit", "storyboard"
            else:
                generation_route, reference_intent = choose_image_generation_route(mode, local_reference_slots)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        reference_preprocess_mode = fields.get("reference_preprocess", "none")
        if reference_preprocess_mode not in {"none", "person_cutout"}:
            return json_response({"error": "未知的参考图预处理方式。"}, status=400)
        if generation_route == "t2i":
            reference_preprocess_mode = "none"
        prompt_text = fields.get("prompt", "").strip()
        character_lock = fields.get("character_lock", "")
        negative_prompt = fields.get("negative_prompt", "").strip()
        if not prompt_text:
            return json_response({"error": "请先输入角色或画面提示词。"}, status=400)
        if len(prompt_text) > 5000 or len(character_lock) > 2000 or len(negative_prompt) > 8000:
            return json_response({"error": "提示词、人物固定描述或负面提示词过长。"}, status=400)
        if generation_route == "t2i" or storyboard:
            aspect = fields.get("aspect", "portrait")
            if aspect not in IMAGE_CANVASES:
                return json_response({"error": "未知的图片方向。"}, status=400)
        else:
            # Qwen Edit derives its nearest supported canvas from reference 1
            # through FluxKontextImageScale. Recording a requested aspect here
            # would falsely imply that the selector changed the output.
            aspect = "reference"
        quality = fields.get("quality", "draft")
        if quality not in {"draft", "standard"}:
            return json_response({"error": "未知的图片质量档位。"}, status=400)
        seed_text = fields.get("seed", "").strip()
        try:
            seed = int(seed_text) if seed_text else random.SystemRandom().randrange(0, 2**63 - 1)
        except ValueError:
            return json_response({"error": "随机种子必须是整数。"}, status=400)
        if not 0 <= seed < 2**63:
            return json_response({"error": "随机种子需要在 0 到 2^63-1 之间。"}, status=400)
        image_engine = fields.get("image_engine", "qwen21")
        if image_engine not in {"qwen21", "legacy"}:
            raise ValueError("未知的图片引擎。")
        steps = (25 if quality == "draft" else 40) if image_engine == "qwen21" else (12 if quality == "draft" else 20)
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "status": "starting",
            "stage": "准备图片工作流",
            "progress_value": 0,
            "progress_max": steps,
            "mode": mode,
            "generation_route": generation_route,
            "image_engine": image_engine,
            "reference_intent": reference_intent,
            "quality": quality,
            "aspect": aspect,
            "prompt": compose_character_prompt(prompt_text, character_lock),
            "character_lock": character_lock,
            "negative_prompt": negative_prompt,
            "seed": seed,
            "steps": steps,
            "local_reference_slots": [str(path) if path else None for path in local_reference_slots],
            "source_reference_sha256": source_reference_sha256,
            "normalized_reference_sha256": normalized_reference_sha256,
            "reference_preprocess": {
                "mode": reference_preprocess_mode,
                "model": "BiRefNet-General ONNX" if reference_preprocess_mode == "person_cutout" else None,
                "processed_slots": [],
                "fallback_used": False,
            },
            "reference_policy": {
                "supplied_count": sum(path is not None for path in local_reference_slots),
                "supplied_slots": [
                    f"image{index+1}" if storyboard else REFERENCE_SLOT_NAMES[index]
                    for index, path in enumerate(local_reference_slots)
                    if path is not None
                ],
                "repeated_master_slots": [],
                "duplicated_primary": False,
                "duplicated_master": False,
                "reference_intent": reference_intent,
            },
            "batch_item_id": batch_item_id,
            "batch_relative_path": batch_relative_path,
            "batch_image_preparation": batch_preparation,
            "created_at": time.time(),
            "error": None,
            "cancel_requested": False,
            "cancel_event": asyncio.Event(),
        }
        if storyboard:
            job["storyboard"] = storyboard
            request.app["storyboards"].begin(job, storyboard, request_id)
        request.app["image_jobs"][job_id] = job
        task = asyncio.create_task(run_image_job(request.app, job), name=f"qwen-image-{job_id}")
        request.app["tasks"].add(task)
        task.add_done_callback(request.app["tasks"].discard)
        local_reference_slots = [None, None, None]
        return json_response({"job": public_image_job(job)}, status=202)
    except (ImageBackendError, OSError, ValueError) as error:
        message = error.message if isinstance(error, ImageBackendError) else str(error)
        return json_response({"error": message}, status=400)
    finally:
        for path in local_reference_slots:
            if path is not None:
                path.unlink(missing_ok=True)
        async with request.app["submission_lock"]:
            request.app["submission_state"]["reserved"] = False


async def handle_image_job(request: web.Request) -> web.Response:
    job = request.app["image_jobs"].get(request.match_info["job_id"])
    if not job:
        return json_response({"error": "图片任务不存在。"}, status=404)
    return json_response({"job": public_image_job(job)})


async def handle_cancel_image(request: web.Request) -> web.Response:
    job = request.app["image_jobs"].get(request.match_info["job_id"])
    if not job:
        return json_response({"error": "图片任务不存在。"}, status=404)
    if job["status"] not in ACTIVE_STATUSES:
        return json_response({"cancelled": False, "job": public_image_job(job)})
    job["cancel_requested"] = True
    job["cancel_event"].set()
    job.update(status="cancelling", stage="正在停止图片生成")
    cancelled = False
    cutout_process = job.get("_cutout_process")
    if cutout_process is not None and cutout_process.returncode is None:
        try:
            cutout_process.terminate()
            cancelled = True
        except ProcessLookupError:
            pass
    cancelled = await cancel_remote_image_job(request.app, job.get("remote_prompt_id")) or cancelled
    return json_response({"cancelled": cancelled, "job": public_image_job(job)})


async def handle_index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_ROOT / "index.html")


async def handle_status(request: web.Request) -> web.Response:
    healthy, stats = await comfy_health(request.app["session"])
    models = model_state()
    active = active_job(request.app)
    active_image = active_image_job(request.app)
    prompt_ai = await asyncio.to_thread(prompt_install_state, request.app["prompt_llm"])
    image_models = request.app["image_backend"].verify_models()
    image_models["cutout"] = image_model_state()["cutout"]
    return json_response(
        {
            "ready": healthy and models["ready"] and request.app["model_integrity"]["ready"] and not request.app["missing_nodes"],
            "engine": {"healthy": healthy, "owned": request.app["comfy_owned"], "stats": stats},
            "models": models,
            "model_integrity": request.app["model_integrity"],
            "missing_nodes": request.app["missing_nodes"],
            "active_job": public_job(active) if active else None,
            "active_image_job": public_image_job(active_image) if active_image else None,
            "active_sequence": request.app["sequences"].public(request.app["sequences"].active(), detail=False)
            if request.app["sequences"].active() else None,
            "prompt_ai": prompt_ai,
            "storyboard_prompt_optimizer": True,
            "character_pipeline_workflow": True,
            "storyboard_style_reference": True,
            "storyboard_output_folder": True,
            "active_storyboard_batch": {"id": request.app["storyboard_batches"].current["id"],
                                       "project_id": request.app["storyboard_batches"].current["project_id"]}
            if request.app["storyboard_batches"].current else None,
            "image_models": image_models,
            "csrf_token": request.app["csrf_token"],
        }
    )


async def handle_scan_batch_folder(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_response({"error": "请求不是有效的 JSON。"}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("path"), str):
        return json_response({"error": "请提供本机文件夹路径。"}, status=400)
    try:
        async with request.app["batch_import_lock"]:
            root, files = await batch_import_thread(scan_batch_folder, payload["path"])
            async with request.app["batch_registry_lock"]:
                selection = register_batch_selection(request.app["batch_selections"], root, files)
    except ValueError as error:
        return json_response({"error": str(error)}, status=400)
    response = json_response({"selection": selection})
    response.headers["Cache-Control"] = "no-store"
    return response


async def handle_import_batch_image(request: web.Request) -> web.Response:
    """Prepare browser-selected photos on the local server, one at a time."""
    prepared = None
    registered = False
    try:
        async with request.app["batch_import_lock"]:
            fields = {}
            image_payload = None
            filename = ""
            reader = await request.multipart()
            async for part in reader:
                if part.name == "image" and part.filename:
                    if image_payload is not None:
                        raise ValueError("请逐张导入照片。")
                    filename = PurePosixPath(part.filename.replace("\\", "/")).name
                    if len(filename) > 255 or Path(filename).suffix.lower() not in BATCH_IMAGE_SUFFIXES:
                        raise ValueError("只支持 JPEG、PNG 和 WebP 照片。")
                    chunks, total = [], 0
                    while chunk := await part.read_chunk(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise ValueError("照片超过自动压缩的单张 200MB 输入上限。")
                        chunks.append(chunk)
                    image_payload = b"".join(chunks)
                elif part.name == "selection_id":
                    field = await part.read_chunk(256)
                    if not part.at_eof():
                        raise ValueError("批量上传记录无效。")
                    fields[part.name] = field.decode("ascii")
                else:
                    raise ValueError("未知的批量照片字段。")
            if image_payload is None:
                raise ValueError("请选择照片。")
            append_to = fields.get("selection_id") or None
            if append_to:
                async with request.app["batch_registry_lock"]:
                    existing = request.app["batch_selections"].get(append_to)
                    if not existing or existing["root"] != BATCH_CACHE_ROOT.resolve() or len(existing["files"]) >= BATCH_SELECTION_MAX_FILES:
                        raise ValueError("批量上传记录已失效或已有 200 张照片，请重新选择。")
            prepared = await batch_import_thread(prepare_photo, image_payload, BATCH_CACHE_ROOT, keep_compatible=True)
            path = Path(prepared["prepared_path"])
            stat = path.stat()
            info = prepared["preparation"]
            item = {**prepared, "path": path, "relative_path": filename, "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns, "device": stat.st_dev, "inode": stat.st_ino,
                    "source_sha256": prepared["prepared_sha256"], "width": info["width"], "height": info["height"]}
            async with request.app["batch_registry_lock"]:
                selection = register_batch_selection(request.app["batch_selections"], BATCH_CACHE_ROOT.resolve(), [item], append_to)
                registered = True
            response = json_response({"selection": selection})
            response.headers["Cache-Control"] = "no-store"
            return response
    except (ValueError, OSError) as error:
        return json_response({"error": str(error)}, status=400)
    finally:
        if prepared and not registered:
            remove_cached(prepared["prepared_path"], BATCH_CACHE_ROOT)


async def handle_generate(request: web.Request) -> web.Response:
    async with request.app["submission_lock"]:
        current = active_gpu_job(request.app)
        if current or request.app["submission_state"]["reserved"]:
            return json_response(
                {
                    "error": "已有一个本地 AI 任务正在运行，请等待或先停止。",
                    "job": public_job(current) if current and current in request.app["jobs"].values() else None,
                },
                status=409,
            )
        request.app["submission_state"]["reserved"] = True
    try:
        return await handle_generate_reserved(request)
    finally:
        async with request.app["submission_lock"]:
            request.app["submission_state"]["reserved"] = False


async def handle_generate_reserved(request: web.Request) -> web.Response:
    healthy, _ = await comfy_health(request.app["session"])
    if not healthy:
        return json_response({"error": "本地 ComfyUI 引擎没有响应。"}, status=503)
    models = model_state()
    if not models["ready"]:
        return json_response({"error": "模型尚未完整下载。", "missing": models["missing_required"]}, status=503)
    if not request.app["model_integrity"]["ready"]:
        return json_response({"error": "必需模型没有通过 SHA-256 完整性校验。"}, status=503)
    if request.app["missing_nodes"]:
        return json_response({"error": "ComfyUI 缺少工作流节点。", "missing": request.app["missing_nodes"]}, status=503)

    fields: dict[str, str] = {}
    image_payload = b""
    image_suffix = ".png"
    batch_relative_path = None
    batch_preparation = None
    reader = await request.multipart()
    async for part in reader:
        if part.name == "image" and part.filename:
            image_suffix = Path(part.filename).suffix.lower()
            if image_suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                return json_response({"error": "只支持 PNG、JPEG、WebP 图片。"}, status=400)
            chunks = []
            total = 0
            while True:
                chunk = await part.read_chunk(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    return json_response({"error": "图片超过自动压缩的 200MB 输入上限。"}, status=400)
                chunks.append(chunk)
            image_payload = b"".join(chunks)
        else:
            fields[part.name] = await part.text()

    batch_selection_id = fields.get("batch_selection_id", "").strip()
    batch_file_id = fields.get("batch_file_id", "").strip()
    if bool(batch_selection_id) != bool(batch_file_id):
        return json_response({"error": "批量文件凭据不完整，请重新读取文件夹。"}, status=400)
    if image_payload and batch_selection_id:
        return json_response({"error": "不能同时上传图片并指定批量文件。"}, status=400)
    if batch_selection_id:
        try:
            async with request.app["batch_registry_lock"]:
                image_payload, image_suffix, batch_relative_path = await asyncio.to_thread(
                    read_batch_selection_file,
                    request.app["batch_selections"],
                    batch_selection_id,
                    batch_file_id,
                )
                batch_preparation = dict(request.app["batch_selections"][batch_selection_id]["files"][batch_file_id].get("preparation", {}))
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)

    prompt = fields.get("prompt", "").strip()
    if not prompt:
        return json_response({"error": "请先输入提示词。"}, status=400)
    if len(prompt) > 5000:
        return json_response({"error": "提示词超过 5000 字，请适当精简。"}, status=400)
    character_lock = fields.get("character_lock", "")
    if len(character_lock) > 2000:
        return json_response({"error": "人物固定描述超过 2000 字，请适当精简。"}, status=400)
    seed_text = fields.get("seed", "").strip()
    try:
        seed = int(seed_text) if seed_text else random.SystemRandom().randrange(0, 2**63 - 1)
    except ValueError:
        return json_response({"error": "随机种子必须是整数。"}, status=400)
    if not 0 <= seed < 2**63:
        return json_response({"error": "随机种子需要在 0 到 2^63-1 之间。"}, status=400)

    continue_from_job_id = fields.get("continue_from_job_id", "").strip()
    continuation_source = None
    continuation_source_sha256 = None
    canvas = None
    continuation_parent = None
    if continue_from_job_id:
        if image_payload:
            return json_response({"error": "接着生成会使用上一段视频，不能同时上传参考图片。"}, status=400)
        try:
            continuation_parent = request.app["sequences"].parent(continue_from_job_id)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        source_value = continuation_parent.get("output")
        if not source_value:
            return json_response({"error": "上一段结果没有可读取的本地成片。"}, status=400)
        result_root = RESULT_ROOT.resolve()
        source_path = Path(source_value).resolve()
        reference_root = request.app["sequences"].source_root.resolve()
        if (result_root not in source_path.parents and source_path.parent != reference_root) or not source_path.is_file():
            return json_response({"error": "上一段成片已移动或不在本工作台输出目录。"}, status=400)
        try:
            canvas = (int(continuation_parent["width"]), int(continuation_parent["height"]))
        except (KeyError, TypeError, ValueError):
            return json_response({"error": "上一段缺少画面尺寸记录，不能可靠续接。"}, status=400)
        if any(value <= 0 or value % 32 for value in canvas):
            return json_response({"error": "上一段画面尺寸不符合 H3 网格，不能可靠续接。"}, status=400)
        continuation_source = str(source_path)
        continuation_source_sha256 = continuation_parent.get("output_sha256")

    quality = continuation_parent["quality"] if continuation_parent else fields.get("quality", "draft")
    if quality not in MANIFEST["presets"]:
        return json_response({"error": "未知的质量档位。"}, status=400)

    local_image = None
    image_size = None
    image_digest = None
    if image_payload:
        try:
            async with request.app["batch_import_lock"]:
                local_image, image_size, image_digest = await asyncio.to_thread(prepare_image, image_payload, image_suffix)
        except (OSError, ValueError) as error:
            return json_response({"error": str(error)}, status=400)
    if continuation_parent:
        aspect = continuation_parent["aspect"]
        mode = "continue"
    else:
        requested_aspect = fields.get("aspect", "auto")
        aspect = choose_aspect(requested_aspect, image_size)
        mode = "image" if local_image else "text"

    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "status": "starting",
        "stage": "准备工作流",
        "progress_value": 0,
        "progress_max": 20,
        "workflow": f"{aspect}_{mode}",
        "mode": mode,
        "quality": quality,
        "aspect": aspect,
        "prompt": compose_character_prompt(prompt, character_lock),
        "character_lock": character_lock,
        "seed": seed,
        "local_image": str(local_image) if local_image else None,
        "input_image_sha256": image_digest,
        "batch_relative_path": batch_relative_path,
        "batch_image_preparation": batch_preparation,
        "batch_item_id": batch_file_id or None,
        "continuation": bool(continuation_parent),
        "overlap_frames": CONTINUATION_GUIDE_FRAMES if continuation_parent else 0,
        "continue_from_job_id": continue_from_job_id or None,
        "continuation_source": continuation_source,
        "continuation_source_sha256": continuation_source_sha256,
        "canvas": list(canvas) if canvas else None,
        "created_at": time.time(),
        "error": None,
        "cancel_requested": False,
        "cancel_event": asyncio.Event(),
    }
    request.app["jobs"][job_id] = job
    task = asyncio.create_task(run_job(request.app, job), name=f"h3-{job_id}")
    request.app["tasks"].add(task)
    task.add_done_callback(request.app["tasks"].discard)
    return json_response({"job": public_job(job)}, status=202)


async def handle_job(request: web.Request) -> web.Response:
    job = request.app["jobs"].get(request.match_info["job_id"])
    if not job:
        return json_response({"error": "任务不存在。"}, status=404)
    return json_response({"job": public_job(job)})


async def handle_cancel(request: web.Request) -> web.Response:
    job = request.app["jobs"].get(request.match_info["job_id"])
    if not job:
        return json_response({"error": "任务不存在。"}, status=404)
    if job["status"] not in ACTIVE_STATUSES:
        return json_response({"cancelled": False, "job": public_job(job)})
    job["cancel_requested"] = True
    job["cancel_event"].set()
    job.update(status="cancelling", stage="正在停止")
    cancelled = await cancel_remote_job(request.app["session"], job["id"])
    return json_response({"cancelled": cancelled, "job": public_job(job)})


async def handle_result(request: web.Request) -> web.StreamResponse:
    root = RESULT_ROOT.resolve()
    requested_filename = request.match_info["filename"]
    filename = Path(requested_filename).name
    if filename != requested_filename or Path(filename).suffix.lower() not in VIDEO_SUFFIXES | BATCH_IMAGE_SUFFIXES:
        raise web.HTTPNotFound()
    candidate = (root / request.match_info["job_id"] / filename).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(candidate)


async def handle_flat_result(request: web.Request) -> web.StreamResponse:
    root = RESULT_ROOT.resolve()
    requested_filename = request.match_info["filename"]
    filename = Path(requested_filename).name
    if filename != requested_filename or Path(filename).suffix.lower() not in VIDEO_SUFFIXES:
        raise web.HTTPNotFound()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(candidate)


async def handle_open_results(_: web.Request) -> web.Response:
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", str(RESULT_ROOT)])
    return json_response({"opened": True})


async def handle_shutdown(request: web.Request) -> web.Response:
    request.app["stop_event"].set()
    return json_response({"stopping": True})


async def on_startup(app: web.Application) -> None:
    for path in (UPLOAD_ROOT, RESULT_ROOT, LOG_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    app["sequences"].restore()
    app["storyboards"].restore()
    app["storyboard_batches"].restore()
    app["session"] = ClientSession(timeout=ClientTimeout(total=None, connect=30, sock_read=None))
    app["legacy_image_backend"] = ImageBackend(
        app["session"],
        config=ImageBackendConfig(
            comfy_root=Path(CONFIG["image_comfy_root"]),
            python_executable=Path(CONFIG["image_comfy_python"]),
            comfy_url=CONFIG["image_comfy_url"],
            log_path=LOG_ROOT / "image_comfy.log",
            pid_path=DATA_ROOT / "image_comfy.pid",
        ),
    )
    app["image_backend"] = Qwen21Backend(app["session"], config=ImageBackendConfig(
        comfy_root=Path(CONFIG["qwen21_comfy_root"]),
        python_executable=Path(CONFIG["qwen21_comfy_python"]),
        comfy_url="http://127.0.0.1:8190",
        comfy_args=("--listen", "127.0.0.1", "--port", "8190", "--disable-auto-launch", "--reserve-vram", "1.5", "--cache-none", "--disable-all-custom-nodes"),
        log_path=LOG_ROOT / "qwen21_comfy.log", pid_path=DATA_ROOT / "qwen21_comfy.pid"))
    await ensure_comfy(app)
    app["missing_nodes"] = await validate_nodes(app["session"])
    app["model_integrity"] = await asyncio.to_thread(verify_required_models)


async def on_cleanup(app: web.Application) -> None:
    if app["storyboard_batches"].current:
        app["storyboard_batches"].current["pause_requested"] = True
    for batch in app["sequences"].batches.records.values():
        batch["pause_requested"] = True
    for plan in app["sequences"].plans.values():
        plan["pause_requested"] = True
    active_jobs = [job for job in app["jobs"].values() if job.get("status") in ACTIVE_STATUSES]
    for job in active_jobs:
        job["cancel_requested"] = True
        job["cancel_event"].set()
        await cancel_remote_job(app["session"], job["id"])
    active_image_jobs = [job for job in app["image_jobs"].values() if job.get("status") in ACTIVE_STATUSES]
    for job in active_image_jobs:
        job["cancel_requested"] = True
        job["cancel_event"].set()
        await cancel_remote_image_job(app, job.get("remote_prompt_id"))
    for task in list(app["tasks"]):
        task.cancel()
    if app["tasks"]:
        await asyncio.gather(*app["tasks"], return_exceptions=True)
    for selection in app["batch_selections"].values():
        discard_batch_selection(selection)
    app["batch_selections"].clear()
    if app.get("image_backend"):
        await app["image_backend"].stop_owned()
    if app.get("legacy_image_backend"):
        await app["legacy_image_backend"].stop_owned()
    await asyncio.to_thread(app["prompt_llm"].close)
    await app["session"].close()
    process = app.get("comfy_process")
    if app.get("comfy_owned"):
        if process and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(lambda: process.wait(timeout=30))
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        COMFY_PID_FILE.unlink(missing_ok=True)
    log_handle = app.get("comfy_log_handle")
    if log_handle:
        log_handle.close()
    PID_FILE.unlink(missing_ok=True)


@web.middleware
async def local_security(request: web.Request, handler: Any) -> web.StreamResponse:
    hostname = request.host.rsplit(":", 1)[0].strip("[]").lower()
    if hostname not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden(text="Local access only")
    if request.method == "POST" and request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port not in {None, int(CONFIG["studio_port"])}:
                raise web.HTTPForbidden(text="Cross-origin request refused")
        if request.headers.get("X-H3-CSRF") != request.app["csrf_token"]:
            raise web.HTTPForbidden(text="Missing or invalid local request token")
    return await handler(request)


def build_app() -> web.Application:
    app = web.Application(client_max_size=540 * 1024 * 1024, middlewares=[local_security])
    app["jobs"] = {}
    app["image_jobs"] = {}
    app["batch_selections"] = {}
    app["batch_registry_lock"] = asyncio.Lock()
    app["batch_import_lock"] = asyncio.Lock()
    app["tasks"] = set()
    app["gpu_lock"] = asyncio.Lock()
    app["submission_lock"] = asyncio.Lock()
    app["submission_state"] = {"reserved": False}
    app["stop_event"] = asyncio.Event()
    app["comfy_process"] = None
    app["comfy_log_handle"] = None
    app["comfy_owned"] = False
    app["image_backend"] = None
    app["prompt_llm"] = LocalPromptLLM(
        base_url=CONFIG["ollama_url"],
        pid_file=DATA_ROOT / "ollama.pid",
        executable=Path(CONFIG["ollama_executable"]),
        models_dir=Path(CONFIG["ollama_models"]),
        model=PROMPT_MODEL_PROFILES[DEFAULT_PROMPT_PROFILE]["model"],
        models_by_profile={profile_id: profile["model"] for profile_id, profile in PROMPT_MODEL_PROFILES.items()},
    )
    app["missing_nodes"] = []
    app["model_integrity"] = {"ready": False, "files": []}
    app["csrf_token"] = uuid.uuid4().hex
    app["sequences"] = StorySequences(app, sys.modules[__name__])
    app["sequences"].register()
    app["storyboards"] = Storyboards(app, sys.modules[__name__])
    app["storyboards"].register()
    app["image_folder_picker_lock"] = asyncio.Lock()
    app.router.add_post("/api/image-output-folder", image_output.folder_picker)
    app["storyboard_batches"] = StoryboardBatches(app, sys.modules[__name__])
    app["storyboard_batches"].register()
    app.router.add_get("/", handle_index)
    app.router.add_static("/static/", STATIC_ROOT, append_version=False)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/batch-folders/scan", handle_scan_batch_folder)
    app.router.add_post("/api/batch-images/import", handle_import_batch_image)
    app.router.add_post("/api/generate", handle_generate)
    app.router.add_post("/api/optimize-prompt", handle_optimize_prompt)
    app.router.add_post("/api/images/generate", handle_generate_image)
    app.router.add_get("/api/images/jobs/{job_id}", handle_image_job)
    app.router.add_post("/api/images/jobs/{job_id}/cancel", handle_cancel_image)
    app.router.add_get("/api/jobs/{job_id}", handle_job)
    app.router.add_post("/api/jobs/{job_id}/cancel", handle_cancel)
    app.router.add_post("/api/open-results", handle_open_results)
    app.router.add_post("/api/shutdown", handle_shutdown)
    app.router.add_get("/results/{filename}", handle_flat_result)
    app.router.add_get("/results/{job_id}/{filename}", handle_result)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def serve(open_browser: bool) -> None:
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        host = CONFIG["studio_host"]
        port = int(CONFIG["studio_port"])
        site = web.TCPSite(runner, host, port)
        await site.start()
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="ascii")
        url = f"http://{host}:{port}"
        print(f"MiniMax H3 本地视频工作台：{url}", flush=True)
        if open_browser:
            webbrowser.open(url)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, app["stop_event"].set)
            except NotImplementedError:
                pass
        await app["stop_event"].wait()
    finally:
        try:
            await runner.cleanup()
        except RuntimeError:
            if app.get("session") and not app["session"].closed:
                await on_cleanup(app)


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniMax H3 local video studio")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the local UI in a browser")
    args = parser.parse_args()
    asyncio.run(serve(open_browser=not args.no_browser))


if __name__ == "__main__":
    main()
