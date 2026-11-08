"""Durable local shot plans, sequential H3 execution, and imported continuation clips."""
from __future__ import annotations

import asyncio
import copy
import io
import hashlib
import json
import os
import random
import shutil
import subprocess
import time
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from script_planner import MAX_SHOTS, script_chunks, planning_batches
from sequence_settings import SETTING_KEYS, settings, fit_first_frame
from PIL import Image
from sequence_merge import merge_segments

BUSY = {"planning", "running", "pausing", "merging"}


def token(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("任务或参考视频编号无效。") from error


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def joined_thread(function, *args, **kwargs):
    """Don't clean up files or release the GPU while a cancelled worker still uses them."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.gather(worker, return_exceptions=True)
        raise


def normalize_reference(backend, source: Path, target: Path, quality: str) -> dict:
    info = backend.probe_video(source)
    if not info.get("verified"):
        raise ValueError("参考文件没有可解码的视频流。")
    video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    width, height = int(video["width"]), int(video["height"])
    duration = float(info.get("format", {}).get("duration", 0))
    if not 0.95 <= duration <= 3600 or width * height > 40_000_000:
        raise ValueError("参考视频需为 0.95 秒到 1 小时，画面不超过 4000 万像素。")
    aspect = backend.choose_aspect("auto", (width, height))
    canvas = backend.MANIFEST["presets"][quality][aspect]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，无法准备续接视频。")
    intermediate = target.with_name(target.stem + ".normalized.mp4")
    vf = (f"fps=24,scale={canvas[0]}:{canvas[1]}:force_original_aspect_ratio=increase,"
          f"crop={canvas[0]}:{canvas[1]},setsar=1,setpts=PTS-STARTPTS")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", str(max(0, duration - 2)), "-i", str(source),
               "-map", "0:v:0", "-map", "0:a:0?", "-t", "2", "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-af", "asetpts=PTS-STARTPTS", "-c:a", "aac", "-ar", "32000", "-ac", "2",
               "-movflags", "+faststart", str(intermediate)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if result.returncode:
            raise ValueError("参考视频转换失败：" + result.stderr[-1200:])
        tail = backend.prepare_continuation_clip(intermediate, target)
        return {"width": canvas[0], "height": canvas[1], "aspect": aspect, "quality": quality,
                "source_width": width, "source_height": height, "source_seconds": duration,
                "has_audio": tail["has_audio"]}
    finally:
        intermediate.unlink(missing_ok=True)


class StorySequences:
    def __init__(self, app, backend, root: Path | None = None):
        from sequence_batches import SequenceBatches
        self.app, self.b = app, backend
        self.root = root or backend.DATA_ROOT / "sequences"
        self.source_root = self.root / "references"
        self.plans: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.batches = SequenceBatches(self)

    def save(self, plan):
        plan["updated_at"] = time.time()
        atomic_json(self.root / f"{token(plan['id'])}.json", plan)
        if plan.get("output_subdir"):
            directory = self.b.video_result_directory(plan)
            if directory.is_dir():
                atomic_json(directory / "镜头表.json", self.public(plan))

    def active(self):
        batch = self.batches.active()
        if batch:
            plan = self.plans.get(batch["sequence_id"])
            if plan:
                return {**plan, "status": batch["status"], "active_batch_id": batch["id"]}
        return next((p for p in self.plans.values() if p["status"] in BUSY), None)

    def spawn(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.app["tasks"].add(task)
        task.add_done_callback(self.app["tasks"].discard)

    def restore(self):
        self.source_root.mkdir(parents=True, exist_ok=True)
        # Rehydrate finished H3 jobs from existing local sidecars, never from client paths.
        paths = list(self.b.RESULT_ROOT.glob("*.metadata.json"))
        paths.extend((self.b.RESULT_ROOT / "剧本批量").glob("*/*/*.metadata.json"))
        paths.extend((self.b.RESULT_ROOT / "剧本连续").glob("*/*.metadata.json"))
        for path in paths:
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                job_id = token(meta["prompt_id"])
                output = Path(meta["output"]).resolve()
                expected = (self.b.video_result_directory(meta) if meta.get("output_subdir")
                            else self.b.RESULT_ROOT.resolve())
                if output.parent != expected or not output.is_file():
                    continue
                if self.b.result_metadata_path(output).resolve() != path.resolve():
                    continue
                self.app["jobs"][job_id] = {
                    **{key: meta[key] for key in ("width", "height", "quality", "aspect", "seed", "media", "output_sha256", "completed_at")},
                    "id": job_id, "status": "completed", "stage": "已恢复成片", "output": str(output),
                    "filename": output.name, "video_url": (self.b.video_result_url(meta, output.name)
                        if meta.get("output_subdir") else f"/results/{quote(output.name)}"),
                    "continuation": bool(meta.get("continuation")),
                    "sequence_id": meta.get("sequence_id"), "sequence_batch_id": meta.get("sequence_batch_id"),
                    "output_subdir": meta.get("output_subdir"),
                    "steps": meta.get("steps"), "image_fit": meta.get("image_fit"),
                }
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path in self.source_root.glob("*.json"):
            try:
                source = json.loads(path.read_text(encoding="utf-8"))
                source_id = token(source["id"])
                if path.stem != source_id or not (self.source_root / f"{source_id}.mp4").is_file():
                    continue
                self.sources[source_id] = source
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path in self.root.glob("*.json"):
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
                if token(plan["id"]) != path.stem or not isinstance(plan["shots"], list):
                    continue
                if plan["status"] == "merging":
                    plan.update(status="completed", error=None,
                                merge={"status": "interrupted", "error": "合并曾中断，可重试合并；原镜头保留。"})
                elif plan["status"] in BUSY:
                    plan["status"] = "interrupted"
                    plan["error"] = "工作台曾中断。已保留完成镜头，检查后可继续。"
                for shot in plan["shots"]:
                    job = self.app["jobs"].get(shot.get("job_id"))
                    if job and job.get("status") == "completed":
                        shot.update(status="completed", result=self.b.public_job(job))
                    elif shot["status"] == "running":
                        shot["status"] = "interrupted"
                self.plans[plan["id"]] = plan
                self.save(plan)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self.batches.restore()

    def parent(self, value):
        value = token(value)
        if value in self.sources:
            source = self.sources[value]
            path = (self.source_root / f"{value}.mp4").resolve()
            if path.parent != self.source_root.resolve() or not path.is_file():
                raise ValueError("已导入的参考视频不存在，请重新拖入。")
            return {**source, "output": str(path), "status": "completed"}
        job = self.app["jobs"].get(value)
        if not job or job.get("status") != "completed":
            raise ValueError("找不到可续接的上一段成片，请拖入已保存的视频。")
        path = Path(job["output"]).resolve()
        expected = (self.b.video_result_directory(job) if job.get("output_subdir")
                    else self.b.RESULT_ROOT.resolve())
        if path.parent != expected or not path.is_file():
            raise ValueError("上一段成片已移动，请重新拖入。")
        return job

    def public(self, plan, *, detail=True):
        keys = ("id", "title", "status", "error", "created_at", "updated_at", "planned_chunks", "total_chunks",
                "current_job_id", "quality", "aspect", "character_lock", "model_profile", "reference_id",
                "sequence_batch_id", "template_id", "active_batch_id", "steps", "image_fit", "target_shots")
        result = {k: plan.get(k) for k in keys}
        result.update(settings(plan, self.b.MANIFEST, legacy=True))
        result["output_folder"] = str(self.b.video_result_directory(plan)) if plan.get("output_subdir") else None
        result.update(self.first_image_info(plan))
        result["completed_shots"] = sum(s["status"] == "completed" for s in plan["shots"])
        result["total_shots"] = len(plan["shots"])
        result["merge"] = self.public_merge(plan)
        if detail:
            result["script"] = plan["script"]
            result["shots"] = plan["shots"]
        if plan.get("current_job_id") in self.app["jobs"]:
            result["current_job"] = self.b.public_job(self.app["jobs"][plan["current_job_id"]])
        return result

    async def plan_script(self, plan):
        try:
            chunks = planning_batches(plan["script"], plan.get("target_shots", 0))
            async with self.app["gpu_lock"]:
                await self.b.free_comfy_models(self.app["session"], self.b.COMFY_URL)
                await self.app["image_backend"].free()
                for index in range(plan["planned_chunks"], len(chunks)):
                    if plan.get("pause_requested"):
                        plan["status"] = "paused"
                        return
                    units, count = chunks[index]
                    shots = await joined_thread(self.app["prompt_llm"].plan_shots,
                                                units, plan["character_lock"], plan["shots"],
                                                profile=plan["model_profile"], target_shots=count)
                    if len(plan["shots"]) + len(shots) > MAX_SHOTS:
                        raise ValueError("镜头数量超过 1000，请把剧本按集拆开。")
                    plan["shots"].extend(shots)
                    plan["planned_chunks"] = index + 1
                    self.save(plan)
            plan["status"] = "draft"
            plan["error"] = None
        except asyncio.CancelledError:
            plan["status"] = "interrupted"
            raise
        except Exception as error:
            plan.update(status="failed", error=str(error))
        finally:
            self.save(plan)

    def make_job(self, plan, index, parent):
        shot = plan["shots"][index]
        continuation = parent is not None
        job_id = str(uuid.uuid4())
        strategy = plan.get("continuation_strategy", "previous")
        # Imported/older clips may use Turbo. The opt-in R2V model always uses
        # this plan's validated encoder/steps while retaining the source canvas.
        quality = parent["quality"] if parent and strategy == "previous" else plan["quality"]
        aspect = parent["aspect"] if parent else plan["aspect"]
        steps = (parent.get("steps") if parent and strategy == "previous" else plan.get("steps")) or self.b.MANIFEST["presets"][quality]["steps"]
        local_image = None
        if not parent and plan.get("first_image"):
            image_source = (self.root / f"{plan['id']}.png")
            local_image = self.b.UPLOAD_ROOT / f"sequence-{job_id}.png"
            if plan.get("image_fit"):
                with Image.open(image_source) as source:
                    aspect = self.b.choose_aspect(aspect, source.size)
                fitted = fit_first_frame(image_source, tuple(self.b.MANIFEST["presets"][quality][aspect]), plan["image_fit"])
                fitted.save(local_image, format="PNG")
            else:
                shutil.copyfile(image_source, local_image)
        if aspect == "auto":
            aspect = "landscape"
        mode = "continue" if parent else ("image" if local_image else "text")
        identity_image = None
        if strategy == "dual_reference":
            settings(plan, self.b.MANIFEST)
            identity_image = self.root / f"{plan['id']}.png"
            if not plan.get("first_image") or not identity_image.is_file():
                raise ValueError("双参考方案必须保存本剧本的原始角色首图。")
            if parent:
                mode = "continue_dual"
        return {
            "id": job_id, "status": "starting", "stage": f"镜头 {index + 1}：{shot['title']}",
            "progress_value": 0, "progress_max": steps, "workflow": f"{aspect}_{mode}",
            "mode": mode, "quality": quality, "aspect": aspect,
            "steps": steps, "image_fit": plan.get("image_fit"),
            "continuation_strategy": strategy,
            "identity_image": str(identity_image) if continuation and identity_image else None,
            "prompt": self.b.compose_character_prompt(shot["prompt"], plan["character_lock"]),
            "character_lock": plan["character_lock"], "seed": random.SystemRandom().randrange(0, 2**63 - 1),
            "local_image": str(local_image) if local_image else None,
            "continuation": continuation, "overlap_frames": 22 if continuation else 0,
            "continue_from_job_id": parent["id"] if parent else None,
            "continuation_source": parent["output"] if parent else None,
            "continuation_source_sha256": parent.get("output_sha256") if parent else None,
            "canvas": [parent["width"], parent["height"]] if parent else None,
            "sequence_id": plan["id"], "shot_index": index,
            "sequence_batch_id": plan.get("sequence_batch_id"), "output_subdir": plan.get("output_subdir"),
            "batch_relative_path": (f"镜头{index + 1:03d}" if plan.get("sequence_batch_id")
                                    else f"{plan['title']}_镜头{index + 1:03d}"),
            "created_at": time.time(), "error": None, "cancel_requested": False, "cancel_event": asyncio.Event(),
        }

    async def run(self, plan):
        try:
            parent = self.parent(plan["reference_id"]) if plan.get("reference_id") else None
            for index, shot in enumerate(plan["shots"]):
                if shot["status"] == "completed":
                    parent = self.parent(shot["job_id"])
                    continue
                if plan.get("pause_requested"):
                    plan["status"] = "paused"
                    return
                job = self.make_job(plan, index, parent)
                shot.update(status="running", job_id=job["id"])
                plan["current_job_id"] = job["id"]
                # Journal the intended job id before any GPU work; never resubmit an ambiguous request.
                self.save(plan)
                self.app["jobs"][job["id"]] = job
                await self.b.run_job(self.app, job)
                if job["status"] != "completed":
                    shot["status"] = "failed"
                    raise RuntimeError(job.get("error") or "当前镜头已停止，未提交后续镜头。")
                shot.update(status="completed", result=self.b.public_job(job))
                plan["current_job_id"] = None
                self.save(plan)
                parent = job
            plan.update(status="completed", error=None)
            await self.merge_plan(plan)
        except asyncio.CancelledError:
            if plan.get("merge", {}).get("status") != "interrupted":
                plan["status"] = "interrupted"
            raise
        except Exception as error:
            plan.update(status="failed", error=str(error))
        finally:
            self.save(plan)

    def public_merge(self, plan):
        merged = plan.get("merge", {})
        result = {key: merged.get(key) for key in
                  ("status", "error", "filename", "seconds", "frames", "removed_frames", "completed_at")}
        if merged.get("status") == "completed":
            result["video_url"] = f"/api/sequences/{plan['id']}/merged-video"
        return result

    def require_complete(self, plan):
        if (not plan["shots"] or plan["planned_chunks"] != plan["total_chunks"]
                or any(s["status"] != "completed" for s in plan["shots"])):
            raise ValueError("请先完成本首图的全部镜头，再合并完整视频。")

    async def merge_plan(self, plan):
        cancel = threading.Event()
        worker = None
        try:
            self.require_complete(plan)
            plan.update(status="merging", merge={"status": "running", "error": None})
            self.save(plan)
            await joined_thread(self.collect_outputs, plan)
            sources = []
            for shot in plan["shots"]:
                job = self.parent(shot["job_id"])
                path = Path(job["output"])
                sources.append({"id": job["id"], "path": str(path),
                                "metadata_path": str(self.b.result_metadata_path(path))})
            worker = asyncio.create_task(asyncio.to_thread(
                merge_segments, sources, self.b.video_result_directory(plan), cancel))
            plan["merge"] = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel.set()
            if worker:
                await asyncio.gather(worker, return_exceptions=True)
            plan["merge"] = {"status": "interrupted", "error": "合并已停止，可重试；原镜头保留。"}
            raise
        except Exception as error:
            plan["merge"] = {"status": "failed", "error": str(error)}
        finally:
            # Merge errors never turn completed generation into a retryable GPU job.
            plan.update(status="completed", current_job_id=None)
            self.save(plan)

    async def merge_request(self, request):
        try:
            async with self.app["submission_lock"]:
                plan = self.get_plan(request)
                if plan["status"] == "merging":
                    return self.b.json_response({"sequence": self.public(plan)}, status=202)
                await self.check_idle()
                self.require_complete(plan)
                plan.update(status="merging", merge={"status": "running", "error": None})
                self.save(plan)
                self.spawn(self.merge_plan(plan), f"merge-{plan['id']}")
            return self.b.json_response({"sequence": self.public(plan)}, status=202)
        except (ValueError, KeyError, TypeError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def merged_video(self, request):
        try:
            plan = self.get_plan(request)
            merged = plan.get("merge", {})
            directory = self.b.video_result_directory(plan)
            name = merged.get("filename", "")
            path = (directory / name).resolve()
            if (merged.get("status") != "completed" or Path(name).name != name
                    or path.parent != directory or path.suffix != ".mp4" or not path.is_file()):
                raise ValueError("完整视频尚未就绪。")
            return web.FileResponse(path)
        except (ValueError, KeyError, OSError):
            raise web.HTTPNotFound()

    async def check_idle(self):
        if self.b.active_gpu_job(self.app) or self.app["submission_state"]["reserved"]:
            raise ValueError("已有本地 AI 任务正在运行，请完成或暂停后重试。")

    async def create(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象。")
            if set(data) - ({"script", "title", "character_lock", "model_profile"} | SETTING_KEYS):
                raise ValueError("请求包含未知字段。")
            script_chunks(data.get("script"))
            config = settings(data, self.b.MANIFEST)
            chunks = planning_batches(data["script"], config["target_shots"])
            lock = data.get("character_lock", "")
            if not isinstance(lock, str) or len(lock) > 2000:
                raise ValueError("人物固定描述需为文字，且不超过 2000 字。")
            profile_id, _ = self.b.resolve_prompt_model_profile(data.get("model_profile", self.b.DEFAULT_PROMPT_PROFILE))
            if not self.b.prompt_install_state(self.app["prompt_llm"])["profiles"][profile_id]["installed"]:
                return self.b.json_response({"error": "本地文本模型尚未安装完整。"}, status=503)
            title = data.get("title", "剧本连续生成")
            if not isinstance(title, str) or not title.strip() or len(title) > 100:
                raise ValueError("标题需为 1 到 100 字。")
            async with self.app["submission_lock"]:
                await self.check_idle()
                plan = {"id": str(uuid.uuid4()), "title": title.strip(), "script": data["script"],
                        "character_lock": lock, "model_profile": profile_id, **config,
                        "status": "planning", "shots": [], "planned_chunks": 0, "total_chunks": len(chunks),
                        "created_at": time.time(), "error": None, "reference_id": None, "current_job_id": None}
                plan["output_subdir"] = self.group_name(plan)
                self.save(plan)
                self.plans[plan["id"]] = plan
                self.spawn(self.plan_script(plan), f"plan-{plan['id']}")
            return self.b.json_response({"sequence": self.public(plan)}, status=202)
        except (ValueError, TypeError, KeyError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def list_plans(self, _):
        return self.b.json_response({"sequences": [self.public(p, detail=False) for p in
                                    sorted(self.plans.values(), key=lambda p: p["updated_at"], reverse=True)
                                    if not p.get("sequence_batch_id")]})

    def get_plan(self, request):
        return self.plans[token(request.match_info["sequence_id"]) ]

    async def detail(self, request):
        try:
            return self.b.json_response({"sequence": self.public(self.get_plan(request))})
        except (ValueError, KeyError):
            return self.b.json_response({"error": "找不到这份镜头表。"}, status=404)

    async def edit(self, request):
        try:
            plan = self.get_plan(request)
            if plan.get("sequence_batch_id"):
                raise ValueError("批量镜头在导入时冻结；请修改原剧本后创建新的首图批次。")
            data = await request.json()
            async with self.app["submission_lock"]:
                if plan["status"] in BUSY:
                    return self.b.json_response({"error": "请先暂停再编辑镜头。"}, status=409)
                prompts = data.get("prompts") if isinstance(data, dict) else None
                if not isinstance(prompts, list) or len(prompts) != len(plan["shots"]):
                    raise ValueError("镜头数量与已保存的镜头表不一致，请重新加载。")
                for shot, prompt in zip(plan["shots"], prompts):
                    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
                        raise ValueError("每个镜头提示词需为 1 到 5000 字。")
                    if shot["status"] == "completed" and prompt != shot["prompt"]:
                        raise ValueError("已经生成的镜头不能修改。")
                for shot, prompt in zip(plan["shots"], prompts):
                    shot["prompt"] = prompt
                self.save(plan)
            return self.b.json_response({"sequence": self.public(plan)})
        except (ValueError, KeyError, TypeError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def start(self, request):
        try:
            plan = self.get_plan(request)
            if plan.get("sequence_batch_id"):
                raise ValueError("批量首图的镜头由所属批次统一启动，不能单独提交。")
            data = await request.json()
            if not isinstance(data, dict) or set(data) - {"reference_id"}:
                raise ValueError("启动参数无效。")
            async with self.app["submission_lock"]:
                await self.check_idle()
                if plan["status"] == "completed":
                    raise ValueError("这份镜头表已全部生成，不会重复提交。")
                if plan["planned_chunks"] < plan["total_chunks"]:
                    plan.update(status="planning", pause_requested=False, error=None)
                    self.save(plan)
                    self.spawn(self.plan_script(plan), f"plan-{plan['id']}")
                else:
                    await self.check_engine([plan])
                    if data.get("reference_id"):
                        if any(s["status"] == "completed" for s in plan["shots"]):
                            raise ValueError("已开始的序列会自动接着自己的上一镜头，无需更换起始视频。")
                        self.parent(data["reference_id"])
                        plan["reference_id"] = data["reference_id"]
                    if plan.get("reference_id"):
                        self.parent(plan["reference_id"])
                    self.collect_outputs(plan)
                    plan.update(status="running", pause_requested=False, error=None)
                    self.save(plan)
                    self.spawn(self.run(plan), f"sequence-{plan['id']}")
            return self.b.json_response({"sequence": self.public(plan)}, status=202)
        except (ValueError, KeyError, TypeError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def check_engine(self, plans):
        from dual_reference import verify_model
        for plan in plans:
            if plan.get("continuation_strategy") == "dual_reference":
                settings(plan, self.b.MANIFEST)
                if not self.first_image_info(plan)["first_image"]:
                    raise ValueError("双参考方案需先保存原始角色首图。")
                await joined_thread(verify_model, self.b.COMFY_ROOT)
        healthy, _ = await self.b.comfy_health(self.app["session"])
        if not healthy or not self.app["model_integrity"]["ready"] or self.app["missing_nodes"]:
            raise ValueError("H3 引擎或模型尚未就绪。")
        async with self.app["session"].get(f"{self.b.COMFY_URL}/queue") as response:
            response.raise_for_status()
            queue = await response.json()
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise ValueError("H3 引擎仍有任务，请等它结束后恢复，避免重复生成。")
        for plan in plans:
            for shot in plan["shots"]:
                if shot["status"] != "completed" and shot.get("job_id"):
                    async with self.app["session"].get(f"{self.b.COMFY_URL}/history/{token(shot['job_id'])}") as response:
                        response.raise_for_status()
                        history = (await response.json()).get(shot["job_id"], {})
                    if self.b.find_video_asset(history.get("outputs", {})):
                        raise ValueError("中断镜头在引擎中已有输出但尚未归档，请先保存该视频；不会自动重做。")

    def group_name(self, plan):
        stem = f"{self.b.safe_result_stem(plan['title'])[:24]}_{uuid.UUID(plan['id']).hex}"
        return f"剧本连续/{self.b.safe_result_stem(stem)}"

    def collect_outputs(self, plan):
        if plan.get("sequence_batch_id"):
            return
        plan.setdefault("output_subdir", self.group_name(plan))
        directory = self.b.video_result_directory(plan)
        directory.mkdir(parents=True, exist_ok=True)
        source = self.root / f"{plan['id']}.png"
        if source.is_file() and not (directory / "首图.png").exists():
            shutil.copy2(source, directory / "首图.png")
        for shot in plan["shots"]:
            if shot["status"] != "completed":
                continue
            job = self.parent(shot["job_id"])
            source = Path(job["output"])
            destination = directory / source.name
            if source.resolve() == destination.resolve():
                continue
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != hashlib.sha256(source.read_bytes()).hexdigest():
                    raise ValueError("归档文件重名且内容不同，未覆盖。")
            else:
                temporary = destination.with_name(destination.name + ".copying")
                shutil.copy2(source, temporary)
                temporary.replace(destination)
            # This is an archive copy; keep original metadata paths and live job links intact.
            sidecar = self.b.result_metadata_path(source)
            target_meta = self.b.result_metadata_path(destination)
            if sidecar.is_file() and not target_meta.exists():
                shutil.copy2(sidecar, target_meta)
        self.save(plan)

    async def collect(self, request):
        try:
            async with self.app["submission_lock"]:
                await self.check_idle()
                plan = self.get_plan(request)
                await joined_thread(self.collect_outputs, plan)
            return self.b.json_response({"sequence": self.public(plan)})
        except (ValueError, KeyError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def configure(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) - SETTING_KEYS:
                raise ValueError("生成设置字段无效。")
            async with self.app["submission_lock"]:
                await self.check_idle()
                plan = self.get_plan(request)
                if plan.get("sequence_batch_id") or any(s.get("job_id") for s in plan["shots"]):
                    raise ValueError("已经提交或冻结的方案不能改画幅；请复制为新方案。")
                config = settings(data, self.b.MANIFEST)
                if config["target_shots"] != plan.get("target_shots", 0):
                    raise ValueError("更改拆镜数量请点‘按新数量重新拆镜’，原镜头表会保留。")
                plan.update(config)
                self.save(plan)
            return self.b.json_response({"sequence": self.public(plan)})
        except (ValueError, KeyError, TypeError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def copy_plan(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) - (SETTING_KEYS | {"replan"}):
                raise ValueError("复制参数无效。")
            config = settings(data, self.b.MANIFEST)
            replan = data.get("replan", False)
            if type(replan) is not bool:
                raise ValueError("重新拆镜选项无效。")
            async with self.app["submission_lock"]:
                await self.check_idle()
                original = self.get_plan(request)
                if not replan and original["planned_chunks"] != original["total_chunks"]:
                    raise ValueError("拆镜尚未完成，请选择重新拆镜。")
                batches = planning_batches(original["script"], config["target_shots"])
                plan = copy.deepcopy(original)
                for key in ("sequence_batch_id", "template_id", "active_batch_id", "output_subdir", "merge"):
                    plan.pop(key, None)
                plan.update(config, id=str(uuid.uuid4()), title=original["title"][:90] + " · 新方案",
                            status="planning" if replan else "draft", error=None, reference_id=None,
                            current_job_id=None, pause_requested=False, created_at=time.time())
                plan["output_subdir"] = self.group_name(plan)
                if replan:
                    plan.update(shots=[], planned_chunks=0, total_chunks=len(batches))
                else:
                    plan["target_shots"] = original.get("target_shots", 0)
                    for shot in plan["shots"]:
                        shot.update(status="pending", job_id=None)
                        shot.pop("result", None)
                if original.get("first_image"):
                    shutil.copy2(self.root / f"{original['id']}.png", self.root / f"{plan['id']}.png")
                self.save(plan)
                self.plans[plan["id"]] = plan
                if replan:
                    self.spawn(self.plan_script(plan), f"plan-{plan['id']}")
            return self.b.json_response({"sequence": self.public(plan)}, status=202 if replan else 200)
        except (ValueError, KeyError, TypeError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def framing_preview(self, request):
        try:
            plan = self.get_plan(request)
            data = dict(request.query)
            if set(data) - SETTING_KEYS:
                raise ValueError("预览参数无效。")
            for field in ("steps", "target_shots"):
                if field in data:
                    data[field] = int(data[field])
            config = settings({**settings(plan, self.b.MANIFEST, legacy=True), **data}, self.b.MANIFEST)
            source = self.root / f"{plan['id']}.png"
            with Image.open(source) as image:
                aspect = self.b.choose_aspect(config["aspect"], image.size)
            size = tuple(self.b.MANIFEST["presets"][config["quality"]][aspect])
            image = await joined_thread(fit_first_frame, source, size, config["image_fit"])
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return web.Response(body=buffer.getvalue(), content_type="image/png", headers={"Cache-Control":"no-store"})
        except (ValueError, KeyError, TypeError, OSError):
            raise web.HTTPBadRequest(text="请先保存首图并选择有效设置。")

    async def video(self, request):
        try:
            plan = self.get_plan(request)
            name = request.match_info["filename"]
            directory = self.b.video_result_directory(plan)
            candidate = (directory / name).resolve()
            if (Path(name).name != name or candidate.parent != directory or not candidate.is_file()
                    or candidate.suffix.lower() not in self.b.VIDEO_SUFFIXES
                    or not any(s.get("result", {}).get("filename") == name for s in plan["shots"])):
                raise ValueError("视频不存在。")
            return web.FileResponse(candidate)
        except (ValueError, KeyError, OSError):
            raise web.HTTPNotFound()

    async def open_folder(self, request):
        try:
            plan = self.get_plan(request)
            directory = self.b.video_result_directory(plan)
            if not plan.get("output_subdir") or not directory.is_dir():
                raise ValueError("请先生成视频或归集已有成片。")
            self.b.subprocess.Popen(["explorer.exe", str(directory)])
            return self.b.json_response({"opened": True})
        except (ValueError, KeyError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def pause(self, request):
        try:
            plan = self.get_plan(request)
            if plan["status"] in BUSY - {"merging"}:
                plan.update(pause_requested=True, status="pausing")
                self.save(plan)
            return self.b.json_response({"sequence": self.public(plan)})
        except (ValueError, KeyError):
            return self.b.json_response({"error": "找不到这份镜头表。"}, status=404)

    def first_image_info(self, plan):
        path = self.root / f"{plan['id']}.png"
        available = bool(plan.get("first_image") and path.is_file())
        return {"first_image": available, "first_image_url":
                f"/api/sequences/{plan['id']}/first-image?v={path.stat().st_mtime_ns}" if available else None}

    async def first_image_preview(self, request):
        try:
            plan = self.get_plan(request)
            if not self.first_image_info(plan)["first_image"]:
                raise ValueError("没有保存首帧。")
            return web.FileResponse(self.root / f"{plan['id']}.png", headers={"Cache-Control": "no-store"})
        except (ValueError, KeyError, OSError):
            raise web.HTTPNotFound()

    async def remove_first_image(self, request):
        try:
            async with self.app["submission_lock"]:
                plan = self.get_plan(request)
                if plan.get("sequence_batch_id"):
                    raise ValueError("批量首图已冻结，不能单独移除。")
                if plan["status"] in BUSY or any(s.get("job_id") for s in plan["shots"]):
                    raise ValueError("开始生成前才能移除首帧。")
                (self.root / f"{plan['id']}.png").unlink(missing_ok=True)
                plan["first_image"] = False
                self.save(plan)
            return self.b.json_response({"ok": True, **self.first_image_info(plan)})
        except (ValueError, KeyError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def first_image(self, request):
        image = None
        try:
            plan = self.get_plan(request)
            if plan.get("sequence_batch_id"):
                raise ValueError("批量首图已冻结，不能单独替换。")
            if plan["status"] in BUSY or any(s.get("job_id") for s in plan["shots"]):
                raise ValueError("开始生成前才能设置首帧。")
            reader = await request.multipart()
            part = await reader.next()
            if not part or part.name != "image" or not part.filename:
                raise ValueError("请选择首帧图片。")
            payload = bytearray()
            while chunk := await part.read_chunk(1024 * 1024):
                payload.extend(chunk)
                if len(payload) > self.b.MAX_SOURCE_BYTES:
                    raise ValueError("首帧图片超过自动压缩的 200MB 输入上限。")
            async with self.app["batch_import_lock"]:
                image, _, _ = await joined_thread(self.b.prepare_image, bytes(payload), Path(part.filename).suffix)
            async with self.app["submission_lock"]:
                if plan["status"] in BUSY or any(s.get("job_id") for s in plan["shots"]):
                    raise ValueError("序列已经开始，不能更换首帧。")
                target = self.root / f"{plan['id']}.png"
                temporary = target.with_suffix(".png.part")
                try:
                    shutil.copyfile(image, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                plan["first_image"] = True
                plan["reference_id"] = None
                self.save(plan)
            return self.b.json_response({"ok": True, **self.first_image_info(plan)})
        except (ValueError, KeyError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)
        finally:
            if image:
                image.unlink(missing_ok=True)

    async def import_video(self, request):
        source_id = str(uuid.uuid4())
        self.source_root.mkdir(parents=True, exist_ok=True)
        temporary = self.source_root / f".{source_id}.upload"
        target = self.source_root / f"{source_id}.mp4"
        success = False
        try:
            quality, filename, count = "draft", None, 0
            digest = hashlib.sha256()
            reader = await request.multipart()
            async for part in reader:
                if part.name == "quality":
                    quality = await part.text()
                elif part.name == "video" and part.filename and filename is None:
                    filename = Path(part.filename.replace("\\", "/")).name
                    if Path(filename).suffix.lower() not in self.b.VIDEO_SUFFIXES:
                        raise ValueError("支持 MP4、MOV、MKV 和 WebM 视频。")
                    with temporary.open("xb") as handle:
                        while chunk := await part.read_chunk(1024 * 1024):
                            count += len(chunk)
                            if count > 512 * 1024 * 1024:
                                raise ValueError("参考视频超过 512MB，请只导出末尾几秒再拖入。")
                            digest.update(chunk)
                            handle.write(chunk)
                else:
                    raise ValueError("只可上传一个参考视频。")
            if not filename or not count or quality not in self.b.MANIFEST["presets"]:
                raise ValueError("视频为空或质量档位无效。")
            details = await joined_thread(normalize_reference, self.b, temporary, target, quality)
            source = {**details, "id": source_id, "filename": filename, "created_at": time.time(),
                      "source_sha256": digest.hexdigest(), "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                      "video_url": f"/api/continuation-sources/{source_id}/video"}
            atomic_json(self.source_root / f"{source_id}.json", source)
            self.sources[source_id] = source
            success = True
            return self.b.json_response({"source": source}, status=201)
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            return self.b.json_response({"error": str(error)}, status=400)
        finally:
            temporary.unlink(missing_ok=True)
            if not success:
                target.unlink(missing_ok=True)

    async def source_video(self, request):
        try:
            source_id = token(request.match_info["source_id"])
            if source_id not in self.sources:
                raise ValueError("参考视频不存在")
            return web.FileResponse(self.source_root / f"{source_id}.mp4")
        except ValueError:
            raise web.HTTPNotFound()

    def register(self):
        self.batches.register()
        routes = self.app.router
        routes.add_get("/api/sequences", self.list_plans)
        routes.add_post("/api/sequences", self.create)
        routes.add_get("/api/sequences/{sequence_id}", self.detail)
        routes.add_post("/api/sequences/{sequence_id}/edit", self.edit)
        routes.add_post("/api/sequences/{sequence_id}/start", self.start)
        routes.add_post("/api/sequences/{sequence_id}/pause", self.pause)
        routes.add_post("/api/sequences/{sequence_id}/first-image", self.first_image)
        routes.add_get("/api/sequences/{sequence_id}/first-image", self.first_image_preview)
        routes.add_post("/api/sequences/{sequence_id}/first-image/remove", self.remove_first_image)
        routes.add_post("/api/sequences/{sequence_id}/settings", self.configure)
        routes.add_post("/api/sequences/{sequence_id}/copy", self.copy_plan)
        routes.add_post("/api/sequences/{sequence_id}/collect", self.collect)
        routes.add_post("/api/sequences/{sequence_id}/merge", self.merge_request)
        routes.add_get("/api/sequences/{sequence_id}/merged-video", self.merged_video)
        routes.add_post("/api/sequences/{sequence_id}/open-folder", self.open_folder)
        routes.add_get("/api/sequences/{sequence_id}/framing-preview", self.framing_preview)
        routes.add_get("/api/sequences/{sequence_id}/videos/{filename}", self.video)
        routes.add_post("/api/continuation-sources", self.import_video)
        routes.add_get("/api/continuation-sources/{source_id}/video", self.source_video)
