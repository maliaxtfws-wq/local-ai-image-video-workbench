"""Optional per-task image copies; project archives always remain authoritative."""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from aiohttp import web


def validate_folder(value):
    if not isinstance(value, str):
        raise ValueError('保存位置需要是文件夹路径。')
    value = value.strip()
    if not value:
        return ''
    path = Path(value)
    if not path.is_absolute() or value.startswith(('\\\\', '//')):
        raise ValueError('请填写本机完整文件夹路径，例如 E:\\生成图片。')
    path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError('请选择具体文件夹，不要直接选择磁盘根目录。')
    probe = path / ('.studio-write-check-' + uuid.uuid4().hex)
    try:
        path.mkdir(parents=True, exist_ok=True)
        try:
            with probe.open('xb') as handle:
                handle.write(b'ok')
        finally:
            probe.unlink(missing_ok=True)
    except OSError as error:
        raise ValueError('保存文件夹不可写或路径无效，请选择其他文件夹。') from error
    return str(path)


def copy_result(source, folder, filename, expected_hash):
    target = Path(folder) / filename
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != expected_hash:
        raise ValueError('图片校验失败，项目中的原图已保留。')
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('同名文件内容不同，未覆盖已有图片。')
        return str(target)
    owned = False
    try:
        with target.open('xb') as outgoing, source.open('rb') as incoming:
            owned = True
            shutil.copyfileobj(incoming, outgoing)
    except BaseException:
        if owned:
            target.unlink(missing_ok=True)
        raise
    return str(target)


def choose_folder():
    script_path = Path(__file__).with_name('choose_image_output.ps1')
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1
    result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-STA', '-File', str(script_path)],
                            capture_output=True, timeout=120, encoding='utf-8-sig',
                            startupinfo=startup,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('无法打开文件夹选择窗口，也可以直接填写完整路径。')
    return json.loads(result.stdout.strip())


async def folder_picker(request):
    lock = request.app['image_folder_picker_lock']
    if lock.locked():
        return web.json_response({'error': '文件夹选择窗口已经打开，请先完成选择。'}, status=409)
    try:
        async with lock:
            folder = await asyncio.to_thread(choose_folder)
        return web.json_response({'folder': folder})
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return web.json_response({'error': '未完成文件夹选择，请重试或直接填写完整路径。'}, status=400)
