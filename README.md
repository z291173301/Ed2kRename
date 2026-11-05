# Ed2kRename

基于 ED2K 链接批量重命名视频文件的 Windows 桌面工具（PyQt5）。

## 功能

- 支持多选文件或选择目录（递归包含子目录）批量处理
- 提供极速模式（按文件大小匹配）和精准模式（按 ED2K MD4 哈希匹配）
- 自动将未匹配文件收进 `@未匹配` 目录，匹配成功后自动移回上一级目录
- 处理完成后自动整理 ED2K 文本中的已匹配/未匹配链接

## 本地运行

```bash
pip install -r requirements.txt
python Ed2kRename.py
```

## 本地打包

```bash
pyinstaller -F -w -i logo.ico Ed2kRename.py
```

生成的程序位于 `dist/Ed2kRename.exe`。

## 自动构建与发布

GitHub Actions 工作流：`.github/workflows/build-release.yml`

- 推送到 `main` 或手动触发：自动构建并上传 `Ed2kRename.exe` 构建产物
- 推送 `v*` 标签（例如 `v1.0.0`）：自动创建 GitHub Release 并附带可执行文件

发布新版本：

```bash
git tag v1.0.0
git push origin v1.0.0
```
