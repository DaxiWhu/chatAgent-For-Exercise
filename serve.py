"""FastAPI 服务化入口：流式对话 + 人工审核 resume。

运行：
    uvicorn serve:app --host 0.0.0.0 --port 8000
（开发热重载加 --reload）

端点：
    GET  /health            健康检查
    POST /chat              SSE 流式对话，低分时触发人工审核会下发 need_review 事件
    POST /review            人工审核 resume（accept / revise），同样 SSE 流式返回后续生成
"""
import os
import logging
import warnings
from contextlib import asynccontextmanager
from langchain_chroma import Chroma

import logger
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough

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
PG_CHECKPOINT_URI = os.getenv("PG_CHECKPOINT_URI", "")
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
    if not PG_CHECKPOINT_URI:
        logging.info("未设置 PG_CHECKPOINT_URI，使用内存 checkpointer（重启丢状态）")
        return None
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        conn = psycopg.connect(PG_CHECKPOINT_URI, autocommit=True)
        cp = PostgresSaver(conn)
        cp.setup()
        logging.info("PostgresSaver 初始化成功")
        return cp
    except Exception as e:  # noqa
        logging.warning("PostgresSaver 初始化失败，回退 MemorySaver: %s", e)
        return None


if not AGNES_API_KEY:
    logging.warning("AGNES_API_KEY 未设置，运行时模型调用会失败")

from langchain_openai import ChatOpenAI, OpenAIEmbeddings  # noqa: E402

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

# 获取prompt在chain中传递的prompt最终的内容
def getPrompt(prompt):
    logger.info(f"最后给到LLM的prompt的内容: {prompt}")
    return prompt
# 定义了一个异步函数 lifespan，它接收一个FastAPI应用实例app作为参数。这个函数将管理应用的生命周期，包括启动和关闭时的操作
# 函数在应用启动时执行一些初始化操作，如设置搜索引擎、加载上下文数据、以及初始化问题生成器
# 函数在应用关闭时执行一些清理操作
# @asynccontextmanager 装饰器用于创建一个异步上下文管理器，它允许你在 yield 之前和之后执行特定的代码块，分别表示启动和关闭时的操作
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    # 申明引用全局变量，在函数中被初始化，并在整个应用中使用
    global model, embeddings, vectorstore, prompt, chain, API_TYPE, CHROMADB_DIRECTORY, CHROMADB_COLLECTION_NAME, PROMPT_TEMPLATE_TXT
    global ONEAPI_API_BASE, ONEAPI_CHAT_API_KEY, ONEAPI_CHAT_MODEL, ONEAPI_EMBEDDING_API_KEY, ONEAPI_EMBEDDING_MODEL
    global OPENAI_API_BASE, OPENAI_CHAT_API_KEY, OPENAI_CHAT_MODEL, OPENAI_EMBEDDING_API_KEY, OPENAI_EMBEDDING_MODEL
    # 根据自己实际情况选择调用model和embedding模型类型
    try:
        logger.info("正在初始化模型、实例化Chroma对象、提取prompt模版、定义chain...")
        # （1）根据API_TYPE选择初始化对应的模型
        if API_TYPE == "oneapi":
            # 实例化一个oneapi客户端对象
            model = ChatOpenAI(
                base_url=ONEAPI_API_BASE,
                api_key=ONEAPI_CHAT_API_KEY,
                model=ONEAPI_CHAT_MODEL,  # 本次使用的模型
                # temperature=0,# 发散的程度，一般为0
                # timeout=None,# 服务请求超时
                # max_retries=2,# 失败重试最大次数
            )
            # 实例化embeddings处理模型
            embeddings = OpenAIEmbeddings(
                base_url=ONEAPI_API_BASE,
                api_key=ONEAPI_EMBEDDING_API_KEY,
                model=ONEAPI_EMBEDDING_MODEL,
                deployment=ONEAPI_EMBEDDING_MODEL
            )
        elif API_TYPE == "openai":
            # 实例化一个ChatOpenAI客户端对象
            model = ChatOpenAI(
                base_url=OPENAI_API_BASE,# 请求的API服务地址
                api_key=OPENAI_CHAT_API_KEY,# API Key
                model=OPENAI_CHAT_MODEL,# 本次使用的模型
                # temperature=0,# 发散的程度，一般为0
                # timeout=None,# 服务请求超时
                # max_retries=2,# 失败重试最大次数
            )
            # 实例化embeddings处理模型
            embeddings = OpenAIEmbeddings(
                base_url=OPENAI_API_BASE,# 请求的API服务地址
                api_key=OPENAI_EMBEDDING_API_KEY,# API Key
                model=OPENAI_EMBEDDING_MODEL,
                )

        # （2）实例化Chroma对象
        # 根据自己的实际情况调整persist_directory和collection_name
        vectorstore = Chroma(persist_directory=CHROMADB_DIRECTORY,
                             collection_name=CHROMADB_COLLECTION_NAME,
                             embedding_function=embeddings,
                             )
        # （3）提取prompt模版

        prompt_template = PromptTemplate.from_file(PROMPT_TEMPLATE_TXT)
        # 测试返回的prompt_template对象中提取template的内容
        # logger.info(f"prompt_template的内容: {prompt_template.template}\n")
        prompt = ChatPromptTemplate.from_messages([("human", str(prompt_template.template))])

        # （4）定义chain
        # 将RAG检索放到LangChain的LCEL的chain中执行
        # 这段代码是使用Langchain框架中的`as_retriever`方法创建一个检索器对象
        # LangChain VectorStore对象不是 Runnable 的子类，因此无法集成到LangChain的LCEL的chain中
        # LangChain Retrievers是Runnable，实现了一组标准方法可集成到LCEL的chain中
        # `vectorstore`是一个向量存储对象，用于存储和检索文本数据
        # `as_retriever`方法将向量存储对象转换为一个检索器对象，该对象可以用于搜索与给定查询最相似的文本
        # `search_type`参数设置为"similarity"，表示使用相似度搜索算法
        # `search_kwargs`参数是一个字典，包含搜索算法的参数，这里的`k`参数设置为5，表示只返回与查询最相似的5个结果
        retriever = vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": 5},
        )
        # 定义chain
        # 先构建prompt模版，将用户输入消息直接赋值给prompt模版中的{query}
        # retriever返回值赋值给prompt模版中的{context}
        # 将完整的prompt给到model执行
        chain = {
                    "query": RunnablePassthrough(),
                    "context": retriever
                } | prompt | getPrompt | model
        logger.info("初始化完成")

    except Exception as e:
        logger.error(f"初始化过程中出错: {str(e)}")
        # raise 关键字重新抛出异常，以确保程序不会在错误状态下继续运行
        raise

    # yield 关键字将控制权交还给FastAPI框架，使应用开始运行
    # 分隔了启动和关闭的逻辑。在yield 之前的代码在应用启动时运行，yield 之后的代码在应用关闭时运行
    yield
    # 关闭时执行
    logger.info("正在关闭...")

