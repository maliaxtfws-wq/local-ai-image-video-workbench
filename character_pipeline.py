"""Per-source character preparation followed by frozen-reference storyboard shots."""
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

EXPORT_ROOT = Path.home() / "Pictures" / "AI-Workbench"


def validate_config(project, config):
    if not isinstance(config, dict):
        raise ValueError("人物处理工作流设置无效。")
    role = config.get("primary_role_id")
    shots = config.get("shot_ids")
    if role not in {r["id"] for r in project["characters"]}:
        raise ValueError("请选择工作流的主角。")
    if not isinstance(shots, list) or len(shots) != 4 or len(set(shots)) != 4:
        raise ValueError("请选择四个不同分镜：人物处理、第 2、3、4 幕。")
    shot_map = {s["id"]: s for s in project["shots"]}
    for sid in shots:
        if sid not in shot_map or role not in shot_map[sid]["cast"]:
            raise ValueError("主角必须在四个工作流分镜中出场。")
    return dict(primary_role_id=role, shot_ids=shots)


def expand_rows(project, rows):
    config = validate_config(project, project.get("character_pipeline"))
    expanded = []
    for group, row in enumerate(rows, 1):
        for act, sid in enumerate(config["shot_ids"], 1):
            expanded.append(dict(asset_id=row["asset_id"], shot_id=sid,
                                 target=config["primary_role_id"], group_index=group, act=act,
                                 processed_character=act > 1))
    return expanded


def check_export_directory(batch):
    root = Path(batch["export_dir"])
    root.mkdir(parents=True, exist_ok=True)
    probe = root / (".studio-write-check-" + uuid.uuid4().hex)
    try:
        with probe.open("xb") as handle:
            handle.write(b"ok")
    finally:
        probe.unlink(missing_ok=True)


def archived_result(store, batch, item):
    project = store.read(batch["project_id"])
    run = next((r for r in project["runs"] if r["id"] == item.get("job_id")), None)
    if not run or run["status"] != "completed" or not run.get("filename"):
        raise ValueError("人物处理或分镜结果尚未归档，不能继续后续分镜。")
    source = store.directory(project["id"]) / "results" / run["filename"]
    if not source.is_file():
        raise ValueError("已生成结果文件缺失，请先恢复该文件。")
    return source, run


def bind_processed_reference(store, batch, item):
    if not item.get("depends_on"):
        return
    parent = next(i for i in batch["items"] if i["id"] == item["depends_on"])
    if parent["status"] != "completed":
        raise ValueError("本组第一幕未成功，请先重试人物处理；后续分镜不会使用原图代替。")
    path, run = archived_result(store, batch, parent)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != run["sha256"]:
        raise ValueError("本组人物处理结果已被修改，已停止以免使用错误参考。")
    snapshot = item["snapshot"]
    target = snapshot["batch"]["target"]
    mapping = next(m for m in snapshot["mapping"] if m.get("role_id") == target)
    item["references"][mapping["slot"] - 1] = str(path)
    mapping.update(sha256=digest, source_job_id=parent["job_id"], source="processed_character")
    for role in snapshot["characters"]:
        if role["id"] == target:
            role.update(asset="", processed_reference=run["image_url"])
    snapshot["processed_character"] = dict(source_job_id=parent["job_id"], sha256=digest,
                                           image_url=run["image_url"], role_id=target)
    item["reference_image_url"] = run["image_url"]


def export_result(store, batch, item):
    source, run = archived_result(store, batch, item)
    meta = item["snapshot"]["batch"]
    # Group and job ids disambiguate same-name inputs and retries, in one directory.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(meta["source_name"]).stem).strip(' .')[:60] or 'image'
    number = meta.get('group_index', meta['index'])
    act = f"_幕{meta['act']}" if meta.get('act') else ''
    name = f"{stem}_{batch['id'][:8]}_{number:03d}{act}_{item['job_id'][:8]}.png"
    target = Path(batch["export_dir"]) / name
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != run["sha256"]:
        raise ValueError("生成结果校验失败，原文件保留，未导出。")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"同名结果已存在且内容不同，未覆盖：{target.name}")
    else:
        try:
            with target.open("xb") as handle, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, handle)
        except FileExistsError:
            raise ValueError("导出文件已被其他程序创建，请检查后继续。")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    record = dict(source_name=meta["source_name"], group_index=meta.get("group_index"), act=meta.get("act"),
                  output=str(target), sha256=digest, job_id=item["job_id"], snapshot=item["snapshot"],
                  settings=batch["settings"], seed=item["seed"])
    sidecar = target.with_suffix('.json')
    temporary = sidecar.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(sidecar)
    item.update(export_path=str(target), export_error=None)
