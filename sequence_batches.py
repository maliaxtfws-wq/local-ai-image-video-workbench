"""Durable, serial copies of one shot plan, each with an independent first frame."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import time
import uuid
from pathlib import Path

from aiohttp import web

from story_sequences import atomic_json, joined_thread, token

BATCH_BUSY = {"preparing", "running", "pausing"}
MAX_BATCH_SHOTS = 10000


class SequenceBatches:
    def __init__(self, sequences):
        self.s, self.b, self.app = sequences, sequences.b, sequences.app
        self.records: dict[str, dict] = {}

    @property
    def root(self):
        return self.s.root / "batches"

    def active(self):
        return next((batch for batch in self.records.values() if batch["status"] in BATCH_BUSY), None)

    def save(self, batch):
        batch["updated_at"] = time.time()
        atomic_json(self.root / f"{token(batch['id'])}.json", batch)

    def restore(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("*.json"):
            try:
                batch = json.loads(path.read_text(encoding="utf-8"))
                if token(batch["id"]) != path.stem or batch["sequence_id"] not in self.s.plans:
                    continue
                for item in batch["items"]:
                    child = self.s.plans[token(item["sequence_id"])]
                    if child.get("sequence_batch_id") != batch["id"]:
                        raise ValueError("首图与批次记录不一致。")
                if batch["status"] in BATCH_BUSY:
                    batch.update(status="interrupted", error="工作台曾中断；已保留首图和完成镜头，检查后可继续。")
                self.records[batch["id"]] = batch
                self.save(batch)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def get(self, request):
        return self.records[token(request.match_info["batch_id"])]

    def public(self, batch):
        result = {key: batch.get(key) for key in (
            "id", "sequence_id", "title", "status", "phase", "error", "created_at", "updated_at")}
        items = []
        current_job = None
        for item in batch["items"]:
            child = self.s.plans[item["sequence_id"]]
            shots = child["shots"]
            items.append({"sequence_id": child["id"], "relative_path": item["relative_path"],
                          "status": child["status"] if child.get("first_image") else "pending_import",
                          "first_image": self.s.first_image_info(child)["first_image"],
                          "first_image_url": self.s.first_image_info(child)["first_image_url"],
                          "completed_shots": sum(shot["status"] == "completed" for shot in shots),
                          "total_shots": len(shots),
                          "merge": self.s.public_merge(child),
                          "output_folder": str(self.b.video_result_directory(child)),
                          "videos": [shot["result"] for shot in shots if shot.get("result")]})
            if child.get("current_job_id") in self.app["jobs"]:
                current_job = self.b.public_job(self.app["jobs"][child["current_job_id"]])
        result.update(items=items, imported_images=sum(item["first_image"] for item in items),
                      total_images=len(items), completed_images=sum(item["status"] == "completed" for item in items),
                      completed_shots=sum(item["completed_shots"] for item in items),
                      total_shots=sum(item["total_shots"] for item in items), current_job=current_job,
                      output_folder=str(self.b.RESULT_ROOT / batch["output_dir"]))
        return result

    async def list_for_plan(self, request):
        try:
            plan = self.s.get_plan(request)
            template_id = plan.get("template_id") or plan["id"]
            records = sorted((batch for batch in self.records.values() if batch["sequence_id"] == template_id),
                             key=lambda batch: batch["updated_at"], reverse=True)
            return self.b.json_response({"batches": [self.public(batch) for batch in records]})
        except (ValueError, KeyError):
            return self.b.json_response({"error": "找不到这份镜头表。"}, status=404)

    async def detail(self, request):
        try:
            return self.b.json_response({"batch": self.public(self.get(request))})
        except (ValueError, KeyError):
            return self.b.json_response({"error": "找不到这份首图批次。"}, status=404)

    async def create(self, request):
        try:
            data = await request.json()
            if (not isinstance(data, dict) or set(data) - {"selection_id", "compliance_confirmed"}
                    or data.get("compliance_confirmed") is not True):
                raise ValueError("请确认素材与提示词可合法使用，并提供已导入的图片清单。")
            selection_id = data.get("selection_id")
            if not isinstance(selection_id, str):
                raise ValueError("请先读取首图文件夹或选择图片。")
            async with self.app["submission_lock"]:
                template = self.s.get_plan(request)
                # Retrying an uncertain HTTP response must recover the same batch, not clone it again.
                existing = next((batch for batch in self.records.values()
                                 if batch.get("selection_id") == selection_id), None)
                if existing:
                    if existing["sequence_id"] != template["id"]:
                        raise ValueError("这份图片清单已用于另一个剧本，请重新导入。")
                    return self.b.json_response({"batch": self.public(existing)}, status=200)
                await self.s.check_idle()
                if (template.get("sequence_batch_id") or not template["shots"]
                        or template["planned_chunks"] < template["total_chunks"]):
                    raise ValueError("请先完成剧本拆镜，再导入批量首图。")
                async with self.app["batch_registry_lock"]:
                    self.b.prune_batch_selections(self.app["batch_selections"])
                    selection = self.app["batch_selections"].get(selection_id)
                    if not selection or not selection["files"]:
                        raise ValueError("首图导入记录已失效，请重新选择文件夹。")
                    files = list(selection["files"].values())
                    if len(files) > self.b.BATCH_SELECTION_MAX_FILES or any(item.get("claimed") for item in files):
                        raise ValueError("图片过多或已被提交，请重新导入；每批最多 200 张。")
                    if len(files) * len(template["shots"]) > MAX_BATCH_SHOTS:
                        raise ValueError("本批次超过 10000 个镜头，请把图片文件夹或剧本拆小。")
                    batch_id = str(uuid.uuid4())
                    folder = self.b.safe_result_stem(f"{self.b.safe_result_stem(template['title'])[:24]}_{uuid.UUID(batch_id).hex}")
                    directory = f"剧本批量/{folder}"
                    batch = {"id": batch_id, "sequence_id": template["id"], "title": template["title"],
                             "selection_id": selection_id, "source_root": str(selection["root"]),
                             "output_dir": directory, "status": "preparing", "phase": "importing",
                             "error": None, "created_at": time.time(), "items": []}
                    for index, source in enumerate(files):
                        child = copy.deepcopy(template)
                        child.pop("merge", None)
                        child_id = str(uuid.uuid4())
                        stem = self.b.safe_result_stem(Path(source["relative_path"]).stem)[:32]
                        group = self.b.safe_result_stem(f"{index + 1:03d}_{stem}")
                        child.update(id=child_id, template_id=template["id"], sequence_batch_id=batch_id,
                                     output_subdir=f"{directory}/{group}", status="draft",
                                     first_image=False, reference_id=None, current_job_id=None, error=None,
                                     pause_requested=False, created_at=time.time())
                        for shot in child["shots"]:
                            shot.update(status="pending", job_id=None)
                            shot.pop("result", None)
                        self.s.save(child)
                        self.s.plans[child_id] = child
                        stored = {key: str(value) if isinstance(value, Path) else value for key, value in source.items()}
                        stored["claimed"] = False
                        batch["items"].append({"sequence_id": child_id, "relative_path": source["relative_path"],
                                               "source": stored})
                    self.save(batch)
                    self.records[batch_id] = batch
                    for source in files:
                        source["claimed"] = True
                self.s.spawn(self.prepare(batch), f"sequence-batch-import-{batch_id}")
            return self.b.json_response({"batch": self.public(batch)}, status=202)
        except (ValueError, KeyError, TypeError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    def read_source(self, batch, item):
        root = Path(batch["source_root"]).resolve(strict=True)
        cache = self.b.BATCH_CACHE_ROOT.resolve()
        if root != cache and not any(root == allowed or root.is_relative_to(allowed)
                                     for allowed in self.b.batch_import_roots()):
            raise ValueError("首图文件夹不再位于允许的本机素材目录中。")
        source = dict(item["source"], claimed=False)
        if source.get("prepared_path") and not Path(source["prepared_path"]).is_file():
            source["prepared_path"] = None
        selection_key, file_key = "s" * 43, "f" * 43
        registry = {selection_key: {"root": root, "files": {file_key: source}, "last_used_at": time.time()}}
        return self.b.read_batch_selection_file(registry, selection_key, file_key)

    async def prepare_picture(self, payload, suffix):
        prepared = None
        def work():
            nonlocal prepared
            prepared = self.b.prepare_image(payload, suffix)
            return prepared
        try:
            return await joined_thread(work)
        except asyncio.CancelledError:
            if prepared:
                prepared[0].unlink(missing_ok=True)
            raise

    async def prepare(self, batch):
        image = None
        try:
            for item in batch["items"]:
                child = self.s.plans[item["sequence_id"]]
                if self.s.first_image_info(child)["first_image"]:
                    continue
                if batch.get("pause_requested"):
                    batch["status"] = "paused"
                    return
                async with self.app["batch_import_lock"]:
                    payload, suffix, _ = await joined_thread(self.read_source, batch, item)
                    image, _, _ = await self.prepare_picture(payload, suffix)
                target = self.s.root / f"{child['id']}.png"
                temporary = target.with_suffix(".png.part")
                try:
                    shutil.copyfile(image, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                    image.unlink(missing_ok=True)
                    image = None
                child["first_image"] = True
                self.s.save(child)
                self.save(batch)
            batch.update(status="draft", phase="ready", error=None)
        except asyncio.CancelledError:
            batch.update(status="interrupted", error="导入中断，已保存完成的首图。")
            raise
        except Exception as error:
            batch.update(status="failed", error=str(error))
        finally:
            if image:
                image.unlink(missing_ok=True)
            self.save(batch)

    def write_group(self, child):
        directory = self.b.video_result_directory(child)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "首图.png"
        if not target.exists():
            shutil.copyfile(self.s.root / f"{child['id']}.png", target)
        atomic_json(directory / "镜头表.json", self.s.public(child))

    def write_manifest(self, batch):
        first = self.s.plans[batch["items"][0]["sequence_id"]]
        root = self.b.video_result_directory(first).parent
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(root / "批次说明.json", self.public(batch))

    async def run(self, batch):
        try:
            for item in batch["items"]:
                child = self.s.plans[item["sequence_id"]]
                if child["status"] == "completed":
                    continue
                if batch.get("pause_requested"):
                    batch["status"] = "paused"
                    return
                child.update(status="running", pause_requested=False, error=None)
                self.s.save(child)
                self.write_group(child)
                await self.s.run(child)
                self.write_group(child)
                if child["status"] == "paused":
                    batch["status"] = "paused"
                    return
                if child["status"] != "completed":
                    raise RuntimeError(f"{item['relative_path']}：{child.get('error') or '当前组已停止，未提交后续首图。'}")
                self.save(batch)
            batch.update(status="completed", error=None)
        except asyncio.CancelledError:
            batch.update(status="interrupted", error="工作台退出；已保留完成镜头。")
            raise
        except Exception as error:
            batch.update(status="failed", error=str(error))
        finally:
            self.save(batch)
            self.write_manifest(batch)

    async def start(self, request):
        try:
            async with self.app["submission_lock"]:
                await self.s.check_idle()
                batch = self.get(request)
                if batch["status"] == "completed":
                    raise ValueError("本批次已经完成，不会重复生成。")
                children = [self.s.plans[item["sequence_id"]] for item in batch["items"]]
                importing = any(not self.s.first_image_info(child)["first_image"] for child in children)
                if not importing:
                    await self.s.check_engine(children)
                batch.update(status="preparing" if importing else "running", pause_requested=False, error=None,
                             phase="importing" if importing else "generating")
                self.save(batch)
                self.s.spawn(self.prepare(batch) if importing else self.run(batch), f"sequence-batch-{batch['id']}")
            return self.b.json_response({"batch": self.public(batch)}, status=202)
        except (ValueError, KeyError, TypeError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    async def pause(self, request):
        try:
            batch = self.get(request)
            if batch["status"] in BATCH_BUSY:
                batch.update(pause_requested=True, status="pausing")
                for item in batch["items"]:
                    child = self.s.plans[item["sequence_id"]]
                    if child["status"] in {"running", "pausing"}:
                        child.update(pause_requested=True, status="pausing")
                        self.s.save(child)
                self.save(batch)
            return self.b.json_response({"batch": self.public(batch)})
        except (ValueError, KeyError):
            return self.b.json_response({"error": "找不到这份首图批次。"}, status=404)

    async def video(self, request):
        try:
            batch = self.get(request)
            variant = token(request.match_info["variant_id"])
            if not any(item["sequence_id"] == variant for item in batch["items"]):
                raise ValueError("首图不属于这个批次。")
            child = self.s.plans[variant]
            name = request.match_info["filename"]
            directory = self.b.video_result_directory(child)
            candidate = (directory / name).resolve()
            if (Path(name).name != name or candidate.parent != directory or not candidate.is_file()
                    or candidate.suffix.lower() not in self.b.VIDEO_SUFFIXES
                    or not any(shot.get("result", {}).get("filename") == name for shot in child["shots"])):
                raise ValueError("视频不存在。")
            return web.FileResponse(candidate)
        except (ValueError, KeyError, OSError):
            raise web.HTTPNotFound()

    async def open_folder(self, request):
        try:
            batch = self.get(request)
            child = self.s.plans[batch["items"][0]["sequence_id"]]
            directory = self.b.video_result_directory(child).parent
            if not directory.is_dir():
                raise ValueError("开始生成后才会创建本批次的输出文件夹。")
            self.b.subprocess.Popen(["explorer.exe", str(directory)])
            return self.b.json_response({"opened": True})
        except (ValueError, KeyError, OSError) as error:
            return self.b.json_response({"error": str(error)}, status=400)

    def register(self):
        routes = self.app.router
        routes.add_get("/api/sequences/{sequence_id}/batches", self.list_for_plan)
        routes.add_post("/api/sequences/{sequence_id}/batches", self.create)
        routes.add_get("/api/sequence-batches/{batch_id}", self.detail)
        routes.add_post("/api/sequence-batches/{batch_id}/start", self.start)
        routes.add_post("/api/sequence-batches/{batch_id}/pause", self.pause)
        routes.add_post("/api/sequence-batches/{batch_id}/open-folder", self.open_folder)
        routes.add_get("/api/sequence-batches/{batch_id}/variants/{variant_id}/videos/{filename}", self.video)
