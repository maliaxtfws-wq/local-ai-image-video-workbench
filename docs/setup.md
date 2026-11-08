# 引擎配置

工作台使用单独的 Python 环境提供网页。每个生成引擎还需要自己的 ComfyUI、GPU 版 PyTorch、依赖和模型；仅安装仓库根目录的 requirements.txt 不能生成。推荐先确认视频引擎能单独运行，再按需要添加图片或文字功能。大模型需要较大的磁盘空间；速度与能否运行取决于显存、内存及模型版本。

相对路径以本仓库目录为起点；也可以在 config.json 中填现有安装的绝对路径。Windows 路径可用正斜杠，例如 D:/AI/ComfyUI。示例中的三个引擎目录相互独立。

| 功能 | 路径配置 | 准备内容 |
| --- | --- | --- |
| 视频（启动必需，8189） | video_comfy_root、video_comfy_python | [ComfyUI](https://github.com/Comfy-Org/ComfyUI) 和它的 Python。视频工作流基线是 v0.34.0；需支持 MiniMaxH3ImageToVideo 和 MiniMaxH3AddGuide。按 [models_manifest.json](../models_manifest.json) 的版本、相对目录和文件名，将 required 为 true 的权重放入该引擎的 models 目录；[模型来源](https://huggingface.co/Comfy-Org/MiniMax-H3)。ClipProj 和双参考续拍是实验项，需要额外权重；第一次请选择普通草稿档。 |
| 图片 · 新版（8190） | qwen21_comfy_root、qwen21_comfy_python | 选择支持 TextEncodeQwenImage21、QwenImage21Cache 的 ComfyUI（项目使用过的提交：0b05cd8a3683cbf0c291b1472bb8b64da41de9ae）。从 [Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1) 获取下列三个文件。 |
| 图片 · 原有方案（8188） | image_comfy_root、image_comfy_python | 安装 [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) 到此引擎，并准备 [image_backend.py](../image_backend.py) 的 MODEL_CONTRACTS 所列 GGUF、编码器和 VAE。 |
| 可选功能 | ollama_executable、ollama_models、portrait_cutout_model | 提示词优化和自动拆镜使用本机 Ollama；按配置中的模型标签与摘要安装对应模型。手工填写提示词不需要 Ollama。抠图另需 BiRefNet 权重，并在旧版图片引擎的 Python 中安装 numpy、onnxruntime 和 Pillow。续拍及合并视频需要能在命令行找到 ffmpeg、ffprobe。 |

新版图片引擎的模型布局：

```text
<qwen21_comfy_root>/models/
  diffusion_models/qwen_image_2.1_int8_convrot.safetensors
  text_encoders/qwen3vl_8b_int8_convrot.safetensors
  vae/qwen_image_2.1_vae_bf16.safetensors
```

**已安装 ComfyUI？** 将对应的 root 指向含 main.py 的目录，python 指向实际能运行该引擎的 python.exe。配置中的 runtime_root 则是工作台自己写入项目、日志和结果的位置。保持默认 loopback 地址和端口；不要把它暴露到公网。首次启动等引擎准备好后，浏览器才会打开。

**导入文件夹：** 在项目下建立 inputs 目录并放入素材，或者把自己的素材目录添加到 batch_import_roots。通过页面直接填写路径扫描时，只允许读取这些目录及子目录；浏览器主动选择图片或目录是另一种导入方式。

**找不到环境或模型？** 先检查视频引擎的 main.py、python.exe 和端口，再对照清单检查文件名和目录。启动日志在 runtime_root 下的 studio_data/logs。不要在已有生成任务时重复启动或更换引擎路径。
