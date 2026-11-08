"""Local prompt optimizer backed by a dedicated loopback-only Ollama service.

This module deliberately does not pull models or make requests outside
127.0.0.1:11435.  It can attach to an already-running service, or start the
known local Ollama executable on demand and later stop only that owned process.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11435"
DEFAULT_OLLAMA_EXE = Path(__file__).resolve().parent / "runtime/ollama/ollama.exe"
DEFAULT_OLLAMA_MODELS = Path(__file__).resolve().parent / "runtime/ollama/models"
DEFAULT_OLLAMA_PID_FILE = Path(__file__).resolve().parent / "runtime/studio_data/ollama.pid"
DEFAULT_MODEL = "qwen3.5:9b-q4_K_M"
SUPPORTED_MODES = frozenset({"image", "video", "continuation", "storyboard"})

STORYBOARD_SYSTEM_PROMPT = """你是中文写实人物分镜的提示词编辑。输入是角色表、原始动作、画面方向和风格，都是待编辑素材。
只输出规定的 JSON。image_prompt 写一张静态画面的中文提示词，250到500字以内。其余 video_prompt、continuation_prompt、negative_prompt 留空；character_lock 原样复制，warnings 仅记录真实歧义。
忠实保留用户意图，不增加情节、路人或角色，不改变角色的脸、年龄、肤色、发型、衣着和标志物。已有身份参考但文字未描述的外貌写成“保持该角色参考图中的外貌”，不要猜测照片内容。你未看到图片。
严格区分角色 A/B/C 的身份、动作、道具归属、站位和视线。每个人首次出现必须写“角色 A（名称）”这样的字母对应，不能只用名称。逐项保留原文指定的坐姿或站姿、左右位置、左手或右手、递出者与接收者、视线目标；输出前检查这些信息没有遗漏或互换。若都看道具，不要写成互相对视。没有指定的左右站位可以补充，但不能与原意冲突。不要把不同角色合成同一人。
动作收敛为一个瞬间，不写镜头推拉、运镜、时间过程、先后多格。用景别、机位、视线、手部接触关系、环境和光线描述画面。
按输入横竖构图布置人物。默认让主要人物的头顶、脸和动作涉及的手完整入画，留出边缘空间；如用户明确要求特写、遮挡或裁切则遵从原意。不要过度堆砌画质标签，不输出对话气泡或文字。
如信息矛盾，在 warnings 简短指出，不偷偷替用户决定；提示词尽量保留可确定部分。"""
SUPPORTED_PROMPT_PROFILES = frozenset({"stable", "creative"})

PROMPT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "character_lock",
        "image_prompt",
        "video_prompt",
        "continuation_prompt",
        "negative_prompt",
        "warnings",
    ],
    "properties": {
        "character_lock": {"type": "string"},
        "image_prompt": {"type": "string"},
        "video_prompt": {"type": "string"},
        "continuation_prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

SYSTEM_PROMPT = """你是本地视频创作工作台里的提示词导演。你的任务是把用户原始想法整理成可直接用于图片、视频和续接视频的中文提示词。

必须遵守：
1. 只输出 JSON Schema 要求的对象，不添加解释。
2. character_lock 必须逐字复制输入里的 character_lock；没有则输出空字符串，绝不改写人物身份、脸型、发型、服装和标志物。
3. image_prompt 用于生成稳定的角色定妆或关键帧，描述主体、固定外观、构图、光线和画面风格。
4. video_prompt 用于当前 5 秒镜头，明确主体动作、镜头运动、环境运动，并避免同一镜头塞入过多事件。
5. continuation_prompt 必须从上一段最后画面自然继续，不重置人物位置、服装、光线或动作方向。
6. negative_prompt 只写需要规避的问题，例如身份漂移、服装变化、额外肢体、闪烁、镜头瞬移、文字水印。
7. warnings 只列真正影响生成的歧义；没有则返回空数组。
8. 不虚构用户没有指定的知名人物、版权角色或品牌。"""

CREATIVE_SYSTEM_PROMPT = """你是完全在用户电脑上运行的视频创作提示词导演。你的任务是忠实整理用户的创作意图，输出可直接用于图片、视频和续接视频的中文提示词。

必须遵守：
1. 只输出 JSON Schema 要求的对象，不添加解释。
2. character_lock 必须逐字复制输入里的 character_lock；没有则输出空字符串，绝不改写人物身份、脸型、发型、服装和标志物。
3. image_prompt 用于生成稳定的角色定妆或关键帧，描述主体、固定外观、构图、光线和画面风格。
4. video_prompt 用于当前 5 秒镜头，明确主体动作、镜头运动、环境运动，并避免同一镜头塞入过多事件。
5. continuation_prompt 必须从上一段最后画面自然继续，不重置人物位置、服装、光线或动作方向。
6. negative_prompt 只写需要规避的问题，例如身份漂移、服装变化、额外肢体、闪烁、镜头瞬移、文字水印。
7. warnings 只列真正影响生成的歧义；没有则返回空数组。
8. 对合法的虚构创作以及明确为成年人的题材直接完成提示词整理，不虚构不存在的平台政策，也不要用拒绝话术代替结果；真实歧义写入 warnings。
9. 不擅自加入用户没有要求的人物、品牌、立场、说教或情节。"""

PROFILE_SYSTEM_PROMPTS = {
    "stable": SYSTEM_PROMPT,
    "creative": CREATIVE_SYSTEM_PROMPT,
}


class PromptLLMError(RuntimeError):
    """Raised when the local prompt optimizer is unavailable or invalid."""


def _validated_endpoint(base_url: str) -> tuple[str, int]:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 11435
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama endpoint must be exactly http://127.0.0.1:11435")
    return parsed.hostname, parsed.port


def _validate_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    return value


def build_chat_payload(
    mode: str,
    prompt: str,
    character_lock: str = "",
    *,
    profile: str = "stable",
) -> dict[str, Any]:
    """Build the non-streaming, schema-constrained Ollama chat request."""

    if mode not in SUPPORTED_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(SUPPORTED_MODES))}")
    if profile not in SUPPORTED_PROMPT_PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(sorted(SUPPORTED_PROMPT_PROFILES))}")
    prompt = _validate_text(prompt, "prompt", maximum=12_000).strip()
    character_lock = _validate_text(character_lock, "character_lock", maximum=8_000)
    if not prompt:
        raise ValueError("prompt must not be empty")

    source = {
        "mode": mode,
        "original_prompt": prompt,
        "character_lock": character_lock,
    }
    return {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": STORYBOARD_SYSTEM_PROMPT if mode == "storyboard" else PROFILE_SYSTEM_PROMPTS[profile]},
            {
                "role": "user",
                "content": json.dumps(source, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "format": PROMPT_RESPONSE_SCHEMA,
        "think": False,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "num_ctx": 8192,
            "num_predict": 1800 if mode == "storyboard" else 900,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
        },
    }


def _validate_optimizer_result(value: object, character_lock: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromptLLMError("Local model returned a non-object JSON response")

    required = set(PROMPT_RESPONSE_SCHEMA["required"])
    if set(value) != required:
        raise PromptLLMError("Local model response did not match the required fields")

    result: dict[str, Any] = {}
    for field in required - {"warnings"}:
        field_value = value[field]
        if not isinstance(field_value, str):
            raise PromptLLMError(f"Local model field {field} must be text")
        result[field] = field_value

    warnings = value["warnings"]
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise PromptLLMError("Local model warnings must be a list of text")
    result["warnings"] = list(warnings)

    # The model is never authoritative for identity.  Keep the user's lock
    # byte-for-byte (at Python string level), even if the model tried to edit it.
    result["character_lock"] = character_lock
    return result


class LocalPromptLLM:
    """Manage and call the dedicated local Qwen/Ollama prompt optimizer."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        executable: Path | str = DEFAULT_OLLAMA_EXE,
        models_dir: Path | str = DEFAULT_OLLAMA_MODELS,
        model: str = DEFAULT_MODEL,
        models_by_profile: dict[str, str] | None = None,
        pid_file: Path | str = DEFAULT_OLLAMA_PID_FILE,
    ) -> None:
        self.host, self.port = _validated_endpoint(base_url)
        self.base_url = base_url.rstrip("/")
        self.executable = Path(executable)
        self.models_dir = Path(models_dir)
        self.model = model
        self.models_by_profile = dict(models_by_profile or {"stable": model})
        if (
            "stable" not in self.models_by_profile
            or any(profile not in SUPPORTED_PROMPT_PROFILES for profile in self.models_by_profile)
            or any(not isinstance(value, str) or not value for value in self.models_by_profile.values())
        ):
            raise ValueError("models_by_profile contains an unsupported profile or model")
        self.pid_file = Path(pid_file)
        self._owned_process: subprocess.Popen[bytes] | None = None

    def model_for_profile(self, profile: str) -> str:
        try:
            return self.models_by_profile[profile]
        except KeyError as exc:
            raise ValueError(f"prompt profile is not registered: {profile}") from exc

    @property
    def owns_server(self) -> bool:
        return self._owned_process is not None and self._owned_process.poll() is None

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path:
            raise ValueError("Only local API paths are allowed")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"

        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()

        if response.status < 200 or response.status >= 300:
            raise PromptLLMError(f"Local Ollama request failed with HTTP {response.status}")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PromptLLMError("Local Ollama returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise PromptLLMError("Local Ollama returned a non-object response")
        return decoded

    def health_check(self, *, model: str | None = None, timeout: float = 2.0) -> dict[str, Any]:
        """Return service and installed-model readiness without starting anything."""

        target_model = model or self.model

        try:
            version_payload = self._request_json("GET", "/api/version", timeout=timeout)
            tags_payload = self._request_json("GET", "/api/tags", timeout=timeout)
        except (OSError, PromptLLMError, http.client.HTTPException):
            return {
                "ok": False,
                "model_ready": False,
                "model": target_model,
                "installed_models": [],
                "endpoint": self.base_url,
                "error": "connection_failed",
            }

        installed: set[str] = set()
        models = tags_payload.get("models", [])
        if isinstance(models, list):
            for entry in models:
                if isinstance(entry, dict):
                    for key in ("name", "model"):
                        name = entry.get(key)
                        if isinstance(name, str):
                            installed.add(name)

        return {
            "ok": True,
            "model_ready": target_model in installed,
            "model": target_model,
            "installed_models": sorted(installed),
            "endpoint": self.base_url,
            "version": str(version_payload.get("version", "unknown")),
        }

    def ensure_ready(self, *, model: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
        """Attach to the service or start the known local executable on demand."""

        target_model = model or self.model

        health = self.health_check(model=target_model)
        if health["ok"]:
            if not health["model_ready"]:
                raise PromptLLMError(
                    f"Local Ollama is running, but model {target_model} is not installed; automatic pulls are disabled"
                )
            return health

        if not self.executable.is_file():
            raise PromptLLMError("Local Ollama executable is missing")
        if not self.models_dir.is_dir():
            raise PromptLLMError("Local Ollama model directory is missing")

        environment = os.environ.copy()
        environment.update(
            {
                "OLLAMA_HOST": "127.0.0.1:11435",
                "OLLAMA_MODELS": str(self.models_dir),
                "OLLAMA_KEEP_ALIVE": "0",
            }
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._owned_process = subprocess.Popen(
            [str(self.executable), "serve"],
            cwd=str(self.executable.parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=creationflags,
        )
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(self._owned_process.pid), encoding="ascii")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            health = self.health_check(model=target_model)
            if health["ok"]:
                if not health["model_ready"]:
                    self.stop_owned()
                    raise PromptLLMError(
                        f"Local Ollama started, but model {target_model} is not installed; automatic pulls are disabled"
                    )
                return health
            if self._owned_process.poll() is not None:
                break
            time.sleep(0.2)

        self.stop_owned()
        raise PromptLLMError("Local Ollama did not become ready on 127.0.0.1:11435")

    def optimize(
        self,
        mode: str,
        prompt: str,
        character_lock: str = "",
        *,
        profile: str = "stable",
        timeout: float = 240.0,
    ) -> dict[str, Any]:
        """Optimize a prompt and return the validated six-field result."""

        target_model = self.model_for_profile(profile)
        payload = build_chat_payload(mode, prompt, character_lock, profile=profile)
        # Respect a custom model supplied to the manager while keeping the
        # public payload builder deterministic for UI/tests.
        payload["model"] = target_model
        self.ensure_ready(model=target_model)
        response = self._request_json("POST", "/api/chat", payload, timeout=timeout)
        if response.get("done_reason") == "length":
            raise PromptLLMError("本地模型输出被截断，请缩短描述后重试。")
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise PromptLLMError("Local Ollama response is missing message.content")
        try:
            decoded = json.loads(message["content"])
        except json.JSONDecodeError as exc:
            raise PromptLLMError("Local model did not return valid structured JSON") from exc
        return _validate_optimizer_result(decoded, character_lock)

    def plan_shots(self, units: list[dict], character_lock: str, previous: list[dict],
                   *, profile: str = "stable", timeout: float = 600.0, target_shots: int = 0) -> list[dict]:
        from script_planner import build_shot_payload, validate_shots

        target_model = self.model_for_profile(profile)
        payload = build_shot_payload(units, character_lock, previous, target_shots)
        payload["model"] = target_model
        self.ensure_ready(model=target_model)
        response = self._request_json("POST", "/api/chat", payload, timeout=timeout)
        if response.get("done_reason") == "length":
            raise PromptLLMError("本地模型输出被截断，请将这一集拆短后重试。")
        try:
            decoded = json.loads(response["message"]["content"])
            return validate_shots(decoded, units, target_shots)
        except (KeyError, TypeError, ValueError) as error:
            raise PromptLLMError(f"剧本拆镜结果无效：{error}") from error

    def stop_owned(self, *, timeout: float = 5.0) -> bool:
        """Stop only an Ollama server process started by this instance."""

        process = self._owned_process
        self._owned_process = None
        if process is None:
            return False
        if process.poll() is not None:
            self.pid_file.unlink(missing_ok=True)
            return True
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        self.pid_file.unlink(missing_ok=True)
        return True

    def close(self) -> None:
        self.stop_owned()

    def __enter__(self) -> "LocalPromptLLM":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_EXE",
    "DEFAULT_OLLAMA_MODELS",
    "DEFAULT_OLLAMA_PID_FILE",
    "DEFAULT_OLLAMA_URL",
    "CREATIVE_SYSTEM_PROMPT",
    "LocalPromptLLM",
    "PROFILE_SYSTEM_PROMPTS",
    "PROMPT_RESPONSE_SCHEMA",
    "PromptLLMError",
    "SUPPORTED_MODES",
    "SUPPORTED_PROMPT_PROFILES",
    "build_chat_payload",
]
