"""Durable, single-flight batches using frozen storyboard reference snapshots."""
import asyncio
import copy
import json
import random
import shutil
import time
import uuid
from pathlib import Path

from aiohttp import web
from storyboards import identifier
import character_pipeline
import image_output


class StoryboardBatches:
    def __init__(self, app, studio):
        self.app, self.studio = app, studio
        self.current = None

    @property
    def store(self):
        return self.app["storyboards"]

    def path(self, pid, bid):
        self.store.read(pid)
        return self.store.directory(pid) / "batches" / (identifier(bid) + ".json")

    def read(self, pid, bid):
        path = self.path(pid, bid)
        if not path.is_file():
            raise ValueError("找不到批量任务。")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, batch):
        path = self.path(batch["project_id"], batch["id"])
        path.parent.mkdir(exist_ok=True)
        batch["updated_at"] = time.time()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def active(self):
        return self.current

    def restore(self):
        for path in self.store.root.glob("*/batches/*.json"):
            batch = json.loads(path.read_text(encoding="utf-8"))
            if batch["status"] not in {"running", "pausing"}:
                continue
            runs = {r["id"]: r for r in self.store.read(batch["project_id"])["runs"]}
            for item in batch["items"]:
                if item["status"] == "running":
                    run = runs.get(item.get("job_id"), {})
                    item["status"] = "completed" if run.get("status") == "completed" else "interrupted"
                    item["image_url"] = run.get("image_url")
                    item["error"] = "工作台退出；检查结果后可手动重试。" if item["status"] == "interrupted" else None
            batch.update(status="interrupted", pause_requested=False)
            self.write(batch)

    def create(self, pid, payload):
        bid = identifier(payload["request_id"])
        if self.path(pid, bid).exists():
            return self.read(pid, bid)
        rows = payload.get("items")
        if not isinstance(rows, list) or not rows:
            raise ValueError("请至少选择一张参考图。")
        settings = payload.get("settings", {})
        engine, quality, aspect = (settings.get(k, default) for k, default in
                                   (("image_engine", "qwen21"), ("quality", "draft"), ("aspect", "landscape")))
        if engine not in {"qwen21", "legacy"} or quality not in {"draft", "standard"} or aspect not in self.studio.IMAGE_CANVASES:
            raise ValueError("图片引擎、质量或画面方向无效。")
        seed_text = str(settings.get("seed", "")).strip()
        seed = int(seed_text) if seed_text else None
        if seed is not None and not 0 <= seed < 2**63:
            raise ValueError("随机种子需要在 0 到 2^63-1 之间。")
        project = self.store.read(pid)
        workflow = payload.get("workflow", "independent")
        if workflow not in {"independent", "character_sequence"}:
            raise ValueError("未知的分镜工作流。")
        if workflow == "character_sequence":
            rows = character_pipeline.expand_rows(project, rows)
        items = []
        parents = {}
        # Validate every row before persisting or submitting any GPU work.
        for index, row in enumerate(rows):
            snapshot = self.store.prepare(pid, row["shot_id"], engine, row, project_snapshot=project)
            asset = project["assets"][row["asset_id"]]
            references = snapshot.pop("references")
            if any(not p.is_file() for p in references):
                raise ValueError("参考图文件缺失，请重新导入。")
            snapshot["batch"] = dict(id=bid, index=index + 1, source_name=asset.get("original_name", asset["filename"]), target=row["target"])
            if workflow == "character_sequence":
                snapshot["batch"].update(group_index=row["group_index"], act=row["act"])
            items.append(dict(id=uuid.uuid4().hex, status="queued", snapshot=snapshot,
                references=[str(p) for p in references], asset_id=row["asset_id"],
                seed=seed if seed is not None else random.SystemRandom().randrange(2**63), error=None))
            if workflow == "character_sequence":
                if row["act"] == 1:
                    parents[row["group_index"]] = items[-1]["id"]
                else:
                    items[-1]["depends_on"] = parents[row["group_index"]]
        batch = dict(id=bid, project_id=pid, status="paused", pause_requested=False, created_at=time.time(),
                     settings=dict(image_engine=engine, quality=quality, aspect=aspect), items=items)
        batch["workflow"] = workflow
        if workflow == "character_sequence":
            batch["export_dir"] = str(character_pipeline.EXPORT_ROOT)
        selected_folder = image_output.validate_folder(payload.get("export_dir", ""))
        if selected_folder:
            batch["export_dir"] = selected_folder
        self.write(batch)
        return batch

    def public(self, batch):
        result = copy.deepcopy(batch)
        for item in result["items"]:
            item.pop("references", None)
            live = self.app["image_jobs"].get(item.get("job_id"))
            if live and item["status"] == "running":
                item["stage"] = live.get("stage")
        return result

    async def launch(self, batch, retry=False):
        async with self.app["submission_lock"]:
            if self.current and self.current["id"] == batch["id"] and self.current["project_id"] == batch["project_id"]:
                return self.current
            if self.studio.active_gpu_job(self.app) or self.app["submission_state"]["reserved"]:
                raise ValueError("另一个生成任务正在运行，请完成后再启动本批次。")
            if retry:
                for item in batch["items"]:
                    if item["status"] in {"failed", "interrupted", "cancelled"}:
                        item.update(status="queued", error=None)
            if not any(item["status"] == "queued" or item.get("export_error") or
                       (batch.get("export_dir") and item["status"] == "completed" and not item.get("export_path"))
                       for item in batch["items"]):
                return batch
            if batch.get("export_dir"):
                character_pipeline.check_export_directory(batch)
            batch.update(status="running", pause_requested=False)
            self.write(batch)
            self.current = batch
            task = asyncio.create_task(self.run(batch), name="storyboard-batch-" + batch["id"])
            self.app["tasks"].add(task)
            task.add_done_callback(self.app["tasks"].discard)
            return batch

    def make_job(self, batch, item):
        settings = batch["settings"]
        steps = (25 if settings["quality"] == "draft" else 40) if settings["image_engine"] == "qwen21" else (12 if settings["quality"] == "draft" else 20)
        paths = []
        try:
            for source in item["references"]:
                target = self.studio.UPLOAD_ROOT / (uuid.uuid4().hex + Path(source).suffix)
                paths.append(target)
                shutil.copy2(source, target)
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        snapshot = copy.deepcopy(item["snapshot"])
        return dict(id=str(uuid.uuid4()), status="starting", stage="准备批量分镜", progress_value=0,
            progress_max=steps, mode="storyboard", generation_route="edit", reference_intent="storyboard",
            **settings, prompt=snapshot["prompt"], character_lock="", negative_prompt="", steps=steps,
            seed=item["seed"], local_reference_slots=[str(p) for p in paths] + [None] * max(0, 3-len(paths)), storyboard=snapshot,
            source_reference_sha256={f"image{m['slot']}": m["sha256"] for m in snapshot["mapping"]},
            normalized_reference_sha256={f"image{m['slot']}": m["sha256"] for m in snapshot["mapping"]},
            reference_preprocess=dict(mode="none", model=None, processed_slots=[], fallback_used=False),
            reference_policy=dict(reference_intent="storyboard", supplied_count=len(paths)),
            created_at=time.time(), error=None, cancel_requested=False, cancel_event=asyncio.Event())

    async def run(self, batch):
        item = None
        try:
            for item in batch["items"]:
                if batch["pause_requested"]:
                    break
                if item["status"] == "completed" and batch.get("export_dir"):
                    # A restart or a disk error can leave a finished image waiting
                    # for export. Resume this phase without generating it again.
                    character_pipeline.export_result(self.store, batch, item)
                    self.write(batch)
                if item["status"] != "queued":
                    continue
                if batch.get("workflow") == "character_sequence":
                    character_pipeline.bind_processed_reference(self.store, batch, item)
                job = self.make_job(batch, item)
                item.update(status="running", job_id=job["id"])
                self.write(batch)
                try:
                    self.store.begin(job, item["snapshot"], uuid.uuid4().hex)
                except BaseException:
                    for path in job["local_reference_slots"]:
                        if path:
                            Path(path).unlink(missing_ok=True)
                    raise
                self.app["image_jobs"][job["id"]] = job
                await self.studio.run_image_job(self.app, job)
                run = next((r for r in self.store.read(batch["project_id"])["runs"] if r["id"] == job["id"]), {})
                item.update(status=job["status"], error=job.get("error"), image_url=run.get("image_url"))
                if item["status"] == "completed" and not item["image_url"]:
                    item.update(status="failed", error=job.get("error") or "结果归档未完成，请先检查单张生成记录。")
                self.write(batch)
                if item["status"] != "completed":
                    break
                if batch.get("export_dir"):
                    character_pipeline.export_result(self.store, batch, item)
                    self.write(batch)
            states = {i["status"] for i in batch["items"]}
            batch["status"] = "completed" if states == {"completed"} else ("failed" if states & {"failed", "cancelled", "interrupted"} else "paused")
        except asyncio.CancelledError:
            if item and item["status"] == "running":
                item.update(status="interrupted", error="工作台退出；检查结果后可手动重试。")
            batch["status"] = "interrupted"
            raise
        except Exception as error:
            if item:
                if item["status"] == "completed":
                    item["export_error"] = str(error)
                else:
                    item.update(status="failed", error=str(error))
            batch["status"] = "failed"
        finally:
            batch["pause_requested"] = False
            try:
                self.write(batch)
            finally:
                self.current = None

    def register(self):
        self.app.router.add_get("/api/storyboards/{pid}/batches", self.list_batches)
        self.app.router.add_post("/api/storyboards/{pid}/batches", self.create_batch)
        self.app.router.add_post("/api/storyboards/{pid}/batches/{bid}/{action}", self.action)

    async def list_batches(self, request):
        try:
            pid = request.match_info["pid"]
            self.store.read(pid)
            batches = [json.loads(p.read_text(encoding="utf-8")) for p in (self.store.directory(pid) / "batches").glob("*.json")]
            return web.json_response({"batches": [self.public(b) for b in sorted(batches, key=lambda b: b["created_at"], reverse=True)]})
        except (ValueError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)

    async def create_batch(self, request):
        try:
            batch = self.create(request.match_info["pid"], await request.json())
            # Creation is intentionally separate from launch: a lost response cannot
            # restart a paused/completed batch when the client retries its request id.
            return web.json_response({"batch": self.public(batch)}, status=201)
        except (ValueError, KeyError, TypeError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)

    async def action(self, request):
        try:
            pid, bid, action = (request.match_info[k] for k in ("pid", "bid", "action"))
            batch = self.current if self.current and self.current["id"] == bid and self.current["project_id"] == pid else self.read(pid, bid)
            if action == "pause":
                if batch["status"] in {"running", "pausing"}:
                    batch.update(pause_requested=True, status="pausing")
                    self.write(batch)
            elif action in {"resume", "retry"}:
                batch = await self.launch(batch, retry=action == "retry")
            else:
                raise ValueError("未知的批量操作。")
            return web.json_response({"batch": self.public(batch)})
        except (ValueError, KeyError, TypeError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)
