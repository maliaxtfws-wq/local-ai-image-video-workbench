"""Persistent character references and shot recipes for the local image engine."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from aiohttp import web
import image_output


def identifier(value):
    value = str(value)
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("无效的分镜或素材编号。")
    return value


def text(value, limit, required=False):
    value = str(value or "").strip()
    if len(value) > limit or (required and not value):
        raise ValueError(f"内容不能为空且不能超过 {limit} 字。" if required else f"内容不能超过 {limit} 字。")
    return value


class Storyboards:
    def __init__(self, app, studio, root=None):
        self.app, self.studio = app, studio
        self.root = Path(root or studio.DATA_ROOT / "storyboards")
        self.root.mkdir(parents=True, exist_ok=True)

    def restore(self):
        # Never automatically resubmit a job after a process restart.
        for path in self.root.glob("*/project.json"):
            project = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for run in project.get("runs", []):
                if run["status"] in self.studio.ACTIVE_STATUSES:
                    run.update(status="interrupted", error="工作台曾退出；请先检查已有结果，再手动重新生成。")
                    changed = True
            if changed:
                self.write(project)

    def directory(self, pid):
        return self.root / identifier(pid)

    def read(self, pid):
        path = self.directory(pid) / "project.json"
        if not path.is_file():
            raise ValueError("找不到这个分镜项目。")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, project):
        directory = self.directory(project["id"])
        directory.mkdir(exist_ok=True)
        project["updated_at"] = time.time()
        temporary = directory / "project.tmp"
        temporary.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / "project.json")

    def create(self, title):
        project = dict(id=uuid.uuid4().hex, title=text(title, 100, True), revision=0,
                       style="真人写实摄影，自然皮肤质感", style_asset="", characters=[], shots=[], runs=[], assets={})
        self.write(project)
        return project

    def save(self, pid, payload):
        project = self.read(pid)
        if payload.get("revision") != project["revision"]:
            raise ValueError("项目已在其他窗口修改。请刷新项目后重试；当前输入尚未保存。")
        characters, shots = payload.get("characters", []), payload.get("shots", [])
        if not isinstance(characters, list) or not isinstance(shots, list) or len(characters) > 30 or len(shots) > 200:
            raise ValueError("每个项目最多 30 个角色、200 个分镜。")
        clean_characters, clean_shots, ids = [], [], set()
        for role in characters:
            rid = identifier(role["id"])
            if rid in ids:
                raise ValueError("角色编号重复。")
            ids.add(rid)
            asset = role.get("asset", "")
            if asset and asset not in project["assets"]:
                raise ValueError("角色参考图不属于这个项目。")
            clean_characters.append(dict(id=rid, name=text(role.get("name"), 40, True),
                description=text(role.get("description"), 800), asset=asset))
        shot_ids = set()
        for shot in shots:
            sid = identifier(shot["id"])
            if sid in shot_ids:
                raise ValueError("分镜编号重复。")
            shot_ids.add(sid)
            cast = shot.get("cast", [])
            if not isinstance(cast, list) or len(cast) > 8 or len(set(cast)) != len(cast) or any(r not in ids for r in cast):
                raise ValueError("分镜角色列表无效（每镜最多 8 人）。")
            guide = shot.get("guide", "")
            if guide and guide not in project["assets"]:
                raise ValueError("姿势／构图参考图不属于这个项目。")
            clean_shots.append(dict(id=sid, title=text(shot.get("title"), 100, True), cast=cast,
                action=text(shot.get("action"), 2600), guide=guide))
        style_asset = payload.get("style_asset", project.get("style_asset", ""))
        if not isinstance(style_asset, str) or (style_asset and style_asset not in project["assets"]):
            raise ValueError("画风参考图不属于这个项目，请重新上传。")
        project.update(title=text(payload.get("title"), 100, True), style=text(payload.get("style"), 500), style_asset=style_asset,
                       characters=clean_characters, shots=clean_shots, revision=project["revision"] + 1)
        if payload.get("character_pipeline"):
            from character_pipeline import validate_config
            project["character_pipeline"] = validate_config(project, payload["character_pipeline"])
        self.write(project)
        return project

    def prepare(self, pid, sid, engine="qwen21", override=None, project_snapshot=None):
        project = self.read(pid) if project_snapshot is None else {
            **project_snapshot, "characters": copy.deepcopy(project_snapshot["characters"]),
            "shots": copy.deepcopy(project_snapshot["shots"])}
        shot = next((s for s in project["shots"] if s["id"] == sid), None)
        if not shot or not shot["action"] or not shot["cast"]:
            raise ValueError("请保存分镜、选择出场角色并填写动作和场景。")
        if override:
            if override.get("asset_id") not in project["assets"]:
                raise ValueError("批量参考图不属于这个项目。")
            target = override.get("target")
            if target == "guide":
                shot["guide"] = override["asset_id"]
            elif target in shot["cast"]:
                role = next(r for r in project["characters"] if r["id"] == target)
                role["asset"] = override["asset_id"]
                if override.get("processed_character"):
                    role["description"] = "以本组第一幕处理结果为准，保留处理后的身份、发型和服装；本幕只调整提示词要求的动作、表情与场景"
            else:
                raise ValueError("批量参考图必须对应本镜出场角色或构图参考。")
        roles = {r["id"]: r for r in project["characters"]}
        selected = [roles[r] for r in shot["cast"]]
        references, mapping, lines = [], [], []
        for index, role in enumerate(selected):
            label = chr(65 + index)
            source = "没有照片，请按以下描述创建新人物"
            if role["asset"]:
                asset = project["assets"][role["asset"]]
                references.append(self.directory(pid) / "assets" / asset["filename"])
                source = f"<image{len(references)}>（参考图 {len(references)}）中的人物"
                mapping.append(dict(slot=len(references), role_id=role["id"], name=role["name"], sha256=asset["sha256"]))
            elif not role["description"]:
                raise ValueError(f"角色 {role['name']} 需要照片或外貌描述。")
            lines.append(f"角色 {label}【{role['name']}】：{source}。固定外貌：{role['description'] or '以原始参考照片为准'}。")
        if shot["guide"]:
            asset = project["assets"][shot["guide"]]
            references.append(self.directory(pid) / "assets" / asset["filename"])
            lines.append(f"<image{len(references)}> 只参考姿势、空间布局与构图，不使用其中人物的身份，不增加其中的路人。")
            mapping.append(dict(slot=len(references), purpose="pose_composition", sha256=asset["sha256"]))
        style_asset = project.get("style_asset", "")
        if style_asset:
            asset = project["assets"].get(style_asset)
            if not asset:
                raise ValueError("画风参考图缺失，请重新上传。")
            references.append(self.directory(pid) / "assets" / asset["filename"])
            lines.append(f"<image{len(references)}> 是整个项目共用的画风参考，只提取绘画媒介、线条、色彩、材质和光影风格。"
                         "不使用其中人物的身份、服装、动作、背景内容、文字或水印，也不添加其中的人物。"
                         "人物身份以各自角色参考为准，动作与场景以本镜描述为准。"
                         "画风以此图为主要依据，下方画面风格文字仅作补充；各镜头保持同一画风。")
            mapping.append(dict(slot=len(references), purpose="style", asset_id=style_asset, sha256=asset["sha256"]))
        reference_limit = 10 if engine == "qwen21" else 3
        if len(references) > reference_limit:
            raise ValueError(f"当前引擎每镜最多 {reference_limit} 张参考图（人物、构图、画风合计），当前 {len(references)} 张。请减少参考图或切换新版引擎。")
        if not references:
            raise ValueError("当前人物分镜至少需要一张角色照片。纯文字建角色请使用角色图片页面，选定后上传到角色库。")
        if not any(r["asset"] for r in selected):
            raise ValueError("至少一名出场角色需要原始照片，姿势图和画风图不能代替身份参考。")
        prompt = (f"生成一个完整场景的单幅分镜，不要分格，不要角色设定拼贴。画面主要人物共 {len(selected)} 人，"
                  "每个角色只出现一次；角色之间保持不同身份，不混合面部，不把所有人画成参考图 1 的人。\n"
                  + "\n".join(lines) + f"\n画面风格：{project['style']}\n本镜场景、动作、人物状态与位置：{shot['action']}\n"
                  "按当前分镜重组人物动作和站位，保持各自可辨认的外貌。除非分镜要求，不添加文字、对话框或水印。")
        if len(prompt) > 5000:
            raise ValueError("角色描述与分镜内容合计过长，请精简到 5000 字以内。")
        return dict(project_id=pid, shot_id=sid, project_title=project["title"], shot_title=shot["title"],
                    revision=project["revision"], prompt=prompt, references=references,
                    mapping=mapping, style=project["style"], style_asset=style_asset,
                    characters=copy.deepcopy(selected), shot=copy.deepcopy(shot))

    def begin(self, job, snapshot, request_id):
        project = self.read(snapshot["project_id"])
        if any(r.get("request_id") == request_id for r in project["runs"]):
            raise ValueError("此生成请求已经提交，请查看分镜记录，不要重复提交。")
        project["runs"].append(dict(id=job["id"], shot_id=snapshot["shot_id"], request_id=request_id,
            status="starting", created_at=time.time(), seed=job["seed"], steps=job["steps"],
            snapshot={k: v for k, v in snapshot.items() if k != "references"}, model=job.get("image_engine", "qwen21")))
        self.write(project)

    def finish(self, job):
        snapshot = job.get("storyboard")
        if not snapshot:
            return
        project = self.read(snapshot["project_id"])
        run = next(r for r in project["runs"] if r["id"] == job["id"])
        run.update(status=job["status"], error=job.get("error"), completed_at=time.time())
        if job["status"] == "completed":
            directory = self.directory(project["id"]) / "results"
            directory.mkdir(exist_ok=True)
            target = directory / f"{snapshot['shot_id']}_{job['id']}.png"
            shutil.copy2(job["output"], target)
            run.update(image_url=f"/api/storyboards/{project['id']}/files/results/{target.name}",
                       filename=target.name, sha256=job["output_sha256"])
            if snapshot.get("export_dir"):
                try:
                    run["export_path"] = image_output.copy_result(target, snapshot["export_dir"], target.name, job["output_sha256"])
                except (OSError, ValueError) as error:
                    run["export_error"] = f"另存失败，原图已保存到项目：{error}"
            target.with_suffix(".json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write(project)

    def register(self):
        self.app.router.add_get("/storyboards", self.index)
        self.app.router.add_get("/api/storyboards", self.list_projects)
        self.app.router.add_post("/api/storyboards", self.create_project)
        self.app.router.add_get("/api/storyboards/{pid}", self.get_project)
        self.app.router.add_post("/api/storyboards/{pid}", self.save_project)
        self.app.router.add_post("/api/storyboards/{pid}/assets", self.upload_asset)
        self.app.router.add_get("/api/storyboards/{pid}/files/{kind}/{filename}", self.file)

    async def index(self, request):
        return web.FileResponse(self.studio.STATIC_ROOT / "storyboards.html")

    async def list_projects(self, request):
        projects = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("*/project.json")]
        return web.json_response({"projects": [{k: p[k] for k in ("id", "title", "updated_at")} for p in sorted(projects, key=lambda p: p["updated_at"], reverse=True)]})

    async def create_project(self, request):
        try:
            return web.json_response({"project": self.create((await request.json()).get("title"))})
        except (ValueError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=400)

    async def get_project(self, request):
        try:
            project = self.read(request.match_info["pid"])
            for run in project["runs"]:
                live = self.app["image_jobs"].get(run["id"])
                if live and live["status"] in self.studio.ACTIVE_STATUSES:
                    run.update(status=live["status"], stage=live["stage"])
            return web.json_response({"project": project})
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=404)

    async def save_project(self, request):
        try:
            return web.json_response({"project": self.save(request.match_info["pid"], await request.json())})
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=400)

    async def upload_asset(self, request):
        prepared = None
        incoming = None
        try:
            pid = request.match_info["pid"]
            self.read(pid)
            reader = await request.multipart()
            part = await reader.next()
            if not part or not part.filename:
                raise ValueError("请选择 PNG、JPEG 或 WebP 图片。")
            suffix = Path(part.filename).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("请选择 PNG、JPEG 或 WebP 图片。")
            async with self.app["batch_import_lock"]:
                self.studio.UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
                incoming = self.studio.UPLOAD_ROOT / ("source-" + uuid.uuid4().hex + suffix)
                with incoming.open('xb') as handle:
                    while chunk := await part.read_chunk(1024 * 1024):
                        handle.write(chunk)
                size = digest = None
                def normalize():
                    nonlocal prepared, size, digest
                    prepared, size, digest = self.studio.prepare_image_file(incoming)
                # Join the worker even if the browser disconnects, then clean up.
                await self.studio.joined_thread(normalize)
            project = self.read(pid)
            aid = uuid.uuid4().hex
            directory = self.directory(pid) / "assets"
            directory.mkdir(exist_ok=True)
            filename = aid + prepared.suffix
            shutil.copy2(prepared, directory / filename)
            asset = dict(id=aid, filename=filename, original_name=Path(part.filename).name,
                         sha256=digest, width=size[0], height=size[1],
                         url=f"/api/storyboards/{pid}/files/assets/{filename}")
            project["assets"][aid] = asset
            self.write(project)
            return web.json_response({"asset": asset})
        except (ValueError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)
        finally:
            if prepared:
                prepared.unlink(missing_ok=True)
            if incoming:
                incoming.unlink(missing_ok=True)

    async def file(self, request):
        try:
            directory = self.directory(request.match_info["pid"])
            kind, filename = request.match_info["kind"], request.match_info["filename"]
            if kind not in {"assets", "results"} or not re.fullmatch(r"[a-f0-9_-]+\.(png|jpg|jpeg|webp)", filename):
                raise ValueError("无效路径")
            candidate = directory / kind / filename
            if not candidate.is_file():
                raise ValueError("找不到文件")
            return web.FileResponse(candidate)
        except ValueError:
            raise web.HTTPNotFound()
