"""FastAPI 服务化入口：流式对话 + 人工审核 resume。

运行（在项目根目录执行）：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
（开发热重载加 --reload）

端点：
    GET  /health            健康检查
    POST /chat              SSE 流式对话，低分时触发人工审核会下发 need_review 事件
    POST /review            人工审核 resume（accept / revise），同样 SSE 流式返回后续生成

说明：本文件原名 serve.py，按目录归类迁入 app/。
"""
import os
import logging
import warnings

# 1) 日志/告警配置（放在最前，确保先于任何库日志生效）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
logging.getLogger("httpx").setLevel(logging.WARNING)

# 2) 让 .env 自动生效（可选依赖，装在则启用）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import psycopg  # 确保 psycopg 名字是纯 psycopg 模块（避免被 SQLAlchemy 方言抢占）

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from src.graph import build_graph


# ---------------------------------------------------------------------------
# 配置（全部来自环境变量）
# ---------------------------------------------------------------------------
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
AGNES_API_BASE = os.getenv("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")
AGNES_MODEL = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
# 与 chatAgent.ipynb / .env 统一使用 DB_URL（原先此处叫 PG_CHECKPOINT_URI，导致 .env 取不到值、静默回退内存）
DB_URL = os.getenv("DB_URL", "")
SERVE_API_KEY = os.getenv("SERVE_API_KEY", "")  # 非空则所有请求需带 X-API-Key


app = FastAPI(title="chatAgent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 构建 graph（含可选 PG checkpointer；缺失/失败则回退内存）
# ---------------------------------------------------------------------------
def _build_checkpointer():
    if not DB_URL:
        logging.info("未设置 DB_URL，使用内存 checkpointer（重启丢状态）")
        return None
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        conn = psycopg.connect(DB_URL, autocommit=True)
        cp = PostgresSaver(conn)
        cp.setup()
        logging.info("PostgresSaver 初始化成功")
        return cp
    except Exception as e:  # noqa
        logging.warning("PostgresSaver 初始化失败，回退 MemorySaver: %s", e)
        return None


if not AGNES_API_KEY:
    logging.warning("AGNES_API_KEY 未设置，运行时模型调用会失败")

from langchain_openai import ChatOpenAI  # noqa: E402

_MODEL = ChatOpenAI(
    model=AGNES_MODEL, api_key=AGNES_API_KEY,
    base_url=AGNES_API_BASE, timeout=30, max_retries=3,
)
graph = build_graph(model=_MODEL, checkpointer=_build_checkpointer())


# ---------------------------------------------------------------------------
# 可选鉴权：设了 SERVE_API_KEY 才校验 X-API-Key
# ---------------------------------------------------------------------------
def _check_auth(x_api_key: str = Header(default="")):
    if SERVE_API_KEY and x_api_key != SERVE_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class ChatReq(BaseModel):
    thread_id: str
    message: str


class ReviewReq(BaseModel):
    thread_id: str
    action: str            # accept | revise
    feedback: str = ""


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatReq, x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    config = {"configurable": {"thread_id": req.thread_id}}

    async def event_gen():
        try:
            async for msg, _meta in graph.astream(
                {"messages": [HumanMessage(content=req.message)]},
                config=config, stream_mode="messages",
            ):
                if msg.content:
                    yield {"event": "token", "data": msg.content}
            # 流结束后判断是否停在人工审核
            state = await graph.aget_state(config)
            if state.next and "human_review" in state.next:
                v = state.values
                yield {"event": "need_review", "data": {
                    "draft": v.get("output"),
                    "score": v.get("score"),
                    "feedback": v.get("feedback"),
                }}
            else:
                yield {"event": "done"}
        except Exception as e:  # noqa
            logging.exception("chat 流异常")
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(event_gen())


@app.post("/review")
async def review(req: ReviewReq, x_api_key: str = Header(default="")):
    _check_auth(x_api_key)
    if req.action not in ("accept", "revise"):
        raise HTTPException(status_code=400, detail="action 必须是 accept 或 revise")
    if req.action == "revise" and not req.feedback.strip():
        raise HTTPException(status_code=400, detail="revise 必须填写 feedback")

    config = {"configurable": {"thread_id": req.thread_id}}
    state = await graph.aget_state(config)
    if not (state.next and "human_review" in state.next):
        raise HTTPException(status_code=409, detail="该会话当前不在人工审核状态")

    async def event_gen():
        try:
            async for msg, _meta in graph.astream(
                Command(resume={"action": req.action,
                                "feedback": req.feedback or None}),
                config=config, stream_mode="messages",
            ):
                if msg.content:
                    yield {"event": "token", "data": msg.content}
            st = await graph.aget_state(config)
            if st.next and "human_review" in st.next:
                v = st.values
                yield {"event": "need_review", "data": {
                    "draft": v.get("output"),
                    "score": v.get("score"),
                    "feedback": v.get("feedback"),
                }}
            else:
                yield {"event": "done"}
        except Exception as e:  # noqa
            logging.exception("review 流异常")
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(event_gen())
