"""Local, bounded photo preparation. Never crop or overwrite a source image."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from PIL import Image

MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_SOURCE_PIXELS = 200_000_000
MAX_IMPORT_BYTES = 25 * 1024 * 1024
TARGET_BYTES = 24 * 1024 * 1024
MAX_IMPORT_PIXELS = 40_000_000
OVERSIZE_LONG_EDGE = 4096


def magick_executable() -> str:
    executable = shutil.which("magick")
    if not executable:
        candidates = sorted(Path(os.environ.get("ProgramFiles", "C:/Program Files")).glob("ImageMagick-*/magick.exe"))
        executable = str(candidates[-1]) if candidates else None
    if not executable:
        raise ValueError("自动压缩需要本机 ImageMagick，请检查安装。")
    return executable


def _run(arguments: list[str], temporary: Path) -> str:
    command = [magick_executable(), *arguments]
    environment = os.environ.copy()
    environment["MAGICK_TEMPORARY_PATH"] = str(temporary)
    try:
        result = subprocess.run(command, capture_output=True, timeout=180, env=environment,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as error:
        raise ValueError("照片压缩超时，请将本批次拆小后重试。") from error
    if result.returncode:
        raise ValueError("照片无法完整解码或压缩，请检查文件是否损坏。")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _limits() -> list[str]:
    return ["-limit", "thread", "4", "-limit", "memory", "1GiB",
            "-limit", "map", "2GiB", "-limit", "disk", "2GiB", "-limit", "time", "120"]


def remove_cached(path: str | Path, cache_root: Path) -> None:
    """Only remove flat, task-owned cache files; never follow a user source path."""
    candidate = Path(path)
    if (candidate.parent.resolve() == cache_root.resolve()
            and candidate.name.startswith("batch-") and not candidate.is_symlink()):
        candidate.unlink(missing_ok=True)


def prepare_photo(payload: bytes | Path, cache_root: Path, *, keep_compatible: bool = False) -> dict:
    # File inputs are streamed to disk by the storyboard importer. Decode limits
    # still apply, without requiring a large original to fit into Python memory.
    file_input = isinstance(payload, Path)
    source_size = payload.stat().st_size if file_input else len(payload)
    if not source_size or (not file_input and source_size > MAX_SOURCE_BYTES):
        raise ValueError("照片为空或超过自动压缩的单张 200MB 输入上限。")
    if file_input:
        with payload.open('rb') as handle:
            header = handle.read(12)
    else:
        header = payload[:12]
    if header.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise ValueError("照片真实格式不受支持，请使用 JPEG、PNG 或 WebP。")
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="processing-", dir=cache_root) as directory:
        temporary = Path(directory)
        source = payload if file_input else temporary / ("source" + suffix)
        if not file_input:
            source.write_bytes(payload)
        probe = _run(["identify", *_limits(), "-ping", "-format", "%m|%w|%h|%[orientation]|%[channels]", str(source) + "[0]"], temporary)
        try:
            format_name, width_text, height_text, orientation, channels = probe.split("|")
            width, height = int(width_text), int(height_text)
        except (ValueError, TypeError) as error:
            raise ValueError("无法读取照片尺寸。") from error
        if format_name not in {"JPEG", "PNG", "WEBP"} or min(width, height) < 1:
            raise ValueError("照片格式或尺寸无效。")
        if width * height > MAX_SOURCE_PIXELS:
            raise ValueError("照片超过自动压缩的 2 亿像素输入上限。")
        oriented_width, oriented_height = ((height, width) if orientation in
                                         {"LeftTop", "RightTop", "RightBottom", "LeftBottom"} else (width, height))
        with source.open('rb') as handle:
            original_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
        compressed = source_size > MAX_IMPORT_BYTES or width * height > MAX_IMPORT_PIXELS
        info = {"compressed": compressed, "original_size": source_size,
                "original_width": oriented_width, "original_height": oriented_height,
                "size": source_size, "width": oriented_width, "height": oriented_height,
                "crop": False, "quality": None, "format": format_name}
        prepared_path = None
        if not compressed:
            with Image.open(source) as opened:
                opened.verify()
            if keep_compatible:
                prepared_path = cache_root / f"batch-{uuid.uuid4().hex}{suffix}"
                if file_input:
                    shutil.copy2(source, prepared_path)
                else:
                    os.replace(source, prepared_path)
            return {"prepared_path": prepared_path, "prepared_sha256": original_hash,
                    "source_sha256": original_hash, "preparation": info}

        # A pixel overflow needs resampling; a byte-only overflow first tries
        # JPEG recompression at the original dimensions. Every retry reads the
        # original and fits inside a box; no crop, extent, or forced aspect ratio.
        edge = max(oriented_width, oriented_height)
        if width * height > MAX_IMPORT_PIXELS:
            edge = min(edge, OVERSIZE_LONG_EDGE)
        alpha = channels.split()[0].lower().endswith("a")
        output_suffix = ".png" if alpha else ".jpg"
        candidate = temporary / ("compatible" + output_suffix)
        for scale_attempt in range(7):
            for quality in ([None] if alpha else [95, 90, 85]):
                command = [*_limits(), str(source) + "[0]", "-auto-orient", "-filter", "Lanczos",
                           "-resize", f"{edge}x{edge}>", "+profile", "exif", "+profile", "xmp", "+profile", "iptc"]
                if alpha:
                    command += ["-define", "png:compression-level=9"]
                else:
                    command += ["-sampling-factor", "4:4:4", "-quality", str(quality)]
                _run([*command, str(candidate)], temporary)
                if candidate.stat().st_size <= TARGET_BYTES:
                    with Image.open(candidate) as opened:
                        opened.load()
                        out_width, out_height = opened.size
                        if out_width * out_height > MAX_IMPORT_PIXELS:
                            raise ValueError("压缩后的照片仍然超过像素上限。")
                    prepared_path = cache_root / f"batch-{uuid.uuid4().hex}{output_suffix}"
                    os.replace(candidate, prepared_path)
                    info.update(size=prepared_path.stat().st_size, width=out_width, height=out_height,
                                quality=quality, format="PNG" if alpha else "JPEG")
                    return {"prepared_path": prepared_path,
                            "prepared_sha256": hashlib.sha256(prepared_path.read_bytes()).hexdigest(),
                            "source_sha256": original_hash, "preparation": info}
            edge = max(1, int(edge * 0.8))
        raise ValueError("照片在保持完整画面的压缩后仍超过大小上限，请单独导出后重试。")
