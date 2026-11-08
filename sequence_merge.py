"""Frame-exact H3 overlap removal; immutable sources and atomic, verified masters."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path

VERSION = "h3-frame22-pcm-concat-v1"
FPS = 24
SAMPLE_RATE = 48000


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def command(args, cancel, timeout=600):
    if cancel.is_set():
        raise RuntimeError("合并已停止，原分段保留。")
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.5)
                if process.returncode:
                    raise ValueError("视频合并工具出错：" + stderr.decode("utf-8", "replace")[-1800:])
                return stdout
            except subprocess.TimeoutExpired:
                if cancel.is_set() or time.monotonic() > deadline:
                    raise RuntimeError("合并已停止或超时，原分段保留。")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def probe(path, cancel):
    return json.loads(command(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                               "-show_format", "-of", "json", str(path)], cancel))


def merge_segments(sources: list[dict], directory: Path, cancel: threading.Event | None = None):
    """Sources are server-resolved {id,path,metadata_path}; caller validates sequence membership."""
    cancel = cancel or threading.Event()
    if not sources or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("没有可合并的完整镜头，或 FFmpeg/FFprobe 未安装。")
    records, size, total = [], None, 0
    for index, source in enumerate(sources):
        path = Path(source["path"])
        meta = json.loads(Path(source["metadata_path"]).read_text(encoding="utf-8"))
        source_hash = digest(path)
        if meta.get("prompt_id") != source["id"] or meta.get("output_sha256") != source_hash:
            raise ValueError(f"镜头 {index + 1} 的生成记录或文件校验不一致，未合并。")
        skip = 0
        if index:
            link = meta.get("continuation")
            if (not isinstance(link, dict) or link.get("source_job_id") != sources[index - 1]["id"]
                    or link.get("guide_frames") != 22
                    or link.get("source_output_sha256") != records[-1]["sha256"]):
                raise ValueError(f"镜头 {index + 1} 不是上一段的 22 帧续接，不能自动去重。")
            skip = 22
        info = probe(path, cancel)
        videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
        if len(videos) != 1:
            raise ValueError("分段视频流数量异常。")
        video = videos[0]
        dimensions = (video["width"], video["height"])
        rotation = any(float(s.get("rotation", 0)) % 360 for s in video.get("side_data_list", []))
        if (Fraction(video["r_frame_rate"]) != FPS or Fraction(video["avg_frame_rate"]) != FPS
                or rotation or (size is not None and size != dimensions)):
            raise ValueError("镜头画幅、方向或帧率不一致，请先检查原视频；不会拉伸或猜测裁切。")
        frames = int(video["nb_read_frames"])
        if frames <= skip:
            raise ValueError("分段视频过短，去重后没有画面。")
        size = dimensions
        records.append(dict(id=source["id"], path=str(path), sha256=source_hash,
                            frames=frames, trim_frames=skip, kept_frames=frames-skip,
                            has_audio=any(s.get("codec_type") == "audio" for s in info["streams"])))
        total += frames-skip
    fingerprint = hashlib.sha256(json.dumps({"version":VERSION, "sources":records}, sort_keys=True).encode()).hexdigest()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Recover a previous completed render after an interrupted HTTP response or plan save.
    for receipt in directory.glob(f"完整视频_去重_{fingerprint[:16]}*.merge.json"):
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            output = directory / saved["filename"]
            if (output.parent == directory and output.name == saved["filename"] and output.suffix == ".mp4"
                    and saved["fingerprint"] == fingerprint and digest(output) == saved["sha256"]):
                return saved
        except (OSError, ValueError, KeyError):
            continue
    filename = f"完整视频_去重_{fingerprint[:16]}_{uuid.uuid4().hex[:8]}.mp4"
    output = directory / filename
    with tempfile.TemporaryDirectory(prefix=".h3-merge-", dir=directory) as temp:
        temp = Path(temp)
        list_lines = []
        for index, record in enumerate(records):
            kept = record["kept_frames"]
            seconds = kept / FPS
            segment = temp / f"segment-{index:05d}.mov"
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-threads", "2",
                    "-i", record["path"]]
            if not record["has_audio"]:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_index = "0:a:0" if record["has_audio"] else "1:a:0"
            start_sample = record["trim_frames"] * (SAMPLE_RATE // FPS)
            samples = kept * (SAMPLE_RATE // FPS)
            filters = (
                f"[0:v:0]trim=start_frame={record['trim_frames']}:end_frame={record['frames']},"
                "setpts=N/(24*TB),setsar=1[v];"
                f"[{audio_index}]aresample=48000,atrim=start_sample={start_sample},asetpts=PTS-STARTPTS,"
                f"apad,atrim=end_sample={samples},"
                f"afade=t=in:st=0:d=0.03,afade=t=out:st={max(0, seconds-0.03):.9f}:d=0.03[a]"
            )
            args += ["-filter_complex_threads", "1", "-filter_complex", filters, "-map", "[v]", "-map", "[a]",
                     "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2",
                     "-r", "24", "-fps_mode", "cfr", "-video_track_timescale", "24000",
                     "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-map_metadata", "-1", str(segment)]
            command(args, cancel)
            list_lines += [f"file '{segment.name}'", f"duration {seconds:.12f}"]
        concat = temp / "segments.ffconcat"
        concat.write_text("\n".join(list_lines)+"\n", encoding="utf-8")
        candidate = temp / "master.mp4"
        command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-f", "concat", "-safe", "1",
                 "-i", str(concat), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac",
                 "-b:a", "192k", "-ar", "48000", "-t", f"{total/FPS:.12f}",
                 "-video_track_timescale", "24000", "-movflags", "+faststart", str(candidate)], cancel)
        info = probe(candidate, cancel)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        if (int(video["nb_read_frames"]) != total or abs(float(info["format"]["duration"])-total/FPS) > .05
                or abs(float(audio["duration"])-total/FPS) > .05):
            raise ValueError("合并视频帧数或音画时长校验失败；原分段保留。")
        # A source modified while rendering invalidates the result, never the source.
        if any(digest(r["path"]) != r["sha256"] for r in records):
            raise ValueError("合并期间源文件发生改变，未发布完整视频。")
        if cancel.is_set():
            raise RuntimeError("合并已停止，原分段保留。")
        result = dict(status="completed", version=VERSION, filename=filename, fingerprint=fingerprint,
                      frames=total, fps=FPS, seconds=total/FPS, removed_frames=sum(r["trim_frames"] for r in records),
                      width=size[0], height=size[1], sources=records, sha256=digest(candidate), completed_at=time.time())
        if output.exists():
            raise ValueError("完整视频文件名已存在，拒绝覆盖。")
        os.rename(candidate, output)
        receipt = output.with_suffix(".merge.json")
        temporary = temp / "receipt.json"
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.rename(temporary, receipt)
        return result
