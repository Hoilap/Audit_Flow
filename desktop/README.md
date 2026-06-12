# Audit Workflow Desktop

运行说明（开发环境）

1. 安装 Python 依赖：

```bash
pip install -r requirements.txt
```

2. 安装 Electron（可选全局或使用项目 devDependencies）：

```bash
cd desktop
npm install
npm run start
```

脚本说明：
- `desktop/api.py`：FastAPI 本地后端，暴露文件读写和 git 操作接口。
- `desktop/main.js`：Electron 主进程，启动本地后端并加载 `desktop/renderer/index.html`。
- 前端会通过 `http://127.0.0.1:8000` 与后端通信。

环境变量:
- 若要使用真实 LLM，请在环境中设置 `OPENAI_API_KEY`，或者在 `.env` 文件中添加：

```
OPENAI_API_KEY=your_key_here
```

启动开发（先启动后端，再启动 Electron）:

```bash
# 在项目根目录
pip install -r requirements.txt
cd desktop
npm install
# 在一个终端启动后端（已在 code 中集成，可由 Electron 启动，但调试时可手动运行）
python -m desktop.api
# 在另一个终端启动 Electron 前端
npm run start
```

生成与执行：中间面板的 LLM 可以将生成写回 `outputs/` 任意路径；若选择“生成后执行（仅 .py）”，后端会对生成的 Python 做语法检查并在独立进程中执行（有超时保护）。

打包（示例，需额外配置 electron-packager 或 electron-builder）：

```bash
# 使用 electron-packager 示例
npm install --save-dev electron-packager
npx electron-packager . audit-workflow --platform=win32 --arch=x64
```

