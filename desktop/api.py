"""desktop.api — FastAPI 入口：创建 app、注册路由、启动服务。

所有端点逻辑已拆分到 routes_*.py 模块中，此文件仅负责：
1. 创建 FastAPI app 并配置 CORS
2. 注册所有 APIRouter
3. 启动时执行初始化（init_db、migrate configs）
4. 提供 python -m desktop.api 启动入口
"""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .common import init_db, _migrate_config_files

from .routes_common import router as common_router
from .routes_blm import router as blm_router
from .routes_osm import router as osm_router
from .routes_llm import router as llm_router
from .routes_llm_config import router as llm_config_router
from .routes_agent import router as agent_router

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(common_router)
app.include_router(blm_router)
app.include_router(osm_router)
app.include_router(llm_router)
app.include_router(llm_config_router)
app.include_router(agent_router)

# 启动初始化
init_db()
_migrate_config_files()

if __name__ == "__main__":
    uvicorn.run(
        "desktop.api:app",
        host="127.0.0.1",
        port=8001,
        reload=True,
        reload_dirs=["desktop", "audit_workflow"],
        reload_excludes=[
            "outputs/*",
            "inputs/*",
            "generated_parsers/*",
            "config/*",
            "*.db",
        ],
    )
