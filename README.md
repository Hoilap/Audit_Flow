# Audit Workflow

## 如何启动

### 后端

```
python -m uvicorn desktop.api:app --host 127.0.0.1 --port 8000 --reload
```

### 启动桌面应用

如果你要打开桌面端界面，进入 `desktop` 目录后执行：

```powershell
cd desktop
npm install
npm run start
```

这会启动 Electron 前端，并自动连接本地 FastAPI 后端。

## 接口文档

更多后端端点说明请见： [docs/backend_endpoints.md](docs/backend_endpoints.md)

## LLM / PydanticAI 配置

LLM 交互统一通过 PydanticAI agent 发起，支持 OpenAI 兼容接口。不要把 API key 写进聊天记录或提交到代码库。复制 `.env.example` 为 `.env`，按实际服务填入：

```text
OPENAI_API_KEY=你的key
OPENAI_MODEL=你要使用的模型名
OPENAI_BASE_URL=兼容接口地址
```

也可以在 `config.example.yml` 的 `matching.llm` 中使用 `api_key_env`、`base_url_env`、`model_env` 指向服务商环境变量。然后把需要的 `llm.enabled` 或 `matching.llm.enabled` 改为 `true` 即可。

