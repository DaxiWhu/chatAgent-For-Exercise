# chatAgent 服务镜像
#
# 构建（在**仓库根目录**执行，上下文必须是根目录才能 COPY 到 src/ 与 app/）：
#   docker build -f deploy/app/Dockerfile -t chatagent:0.2.0 .
#
# 瘦身模式（不装 docling，PDF/docx 自动降级到 pdfminer/python-docx；
# 镜像小很多、冷启动快，但表格/版式/OCR 能力会退化）：
#   docker build -f deploy/app/Dockerfile --build-arg INSTALL_DOCLING=0 -t chatagent:slim .
#
# 运行：
#   docker run --rm -p 8000:8000 --env-file .env chatagent:0.2.0

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # docling/opencv 需要的运行时库与模型缓存位置
    HF_HOME=/home/appuser/.cache/huggingface

WORKDIR /app

# libgl1 / libglib2.0-0：opencv（docling 依赖）运行时需要；
# curl：给 HEALTHCHECK 用。
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

# 先只拷贝依赖清单：依赖没变时这层走缓存，不必每次重装几个 GB
#
# `-c requirements.lock` 是关键：requirements.txt 全用 `>=`，只有锁定清单
# 才让"今天的构建"和"下周的构建"装出**同一个依赖树**。缺了它，同一个 commit
# 在不同时间构建可能跑出不同行为 —— 这类故障排查成本极高（代码一样、结果不一样）。
COPY requirements.txt requirements.lock ./
ARG INSTALL_DOCLING=1
# slim 变体的卸载清单**只放 docling 自己的重依赖**，不要顺手把 tiktoken 也删掉。
#
# ⚠️ `tiktoken` 不是 docling 的依赖，是 `langchain-openai` 的**硬依赖**：
#       langchain-openai 1.1.11 requires tiktoken<1.0.0,>=0.7.0
#   而 `langchain_openai` 在**模块导入期**就 `import tiktoken`（base.py:49），
#   我们的导入链是 app.main → src.turn → src.agents.runner → frontline →
#   specialists → tools → src.rag.retrieval → `from langchain_openai import ChatOpenAI`。
#   于是把它卸掉 = **镜像一定起不来**：容器启动即 ModuleNotFoundError，
#   而 /health/live 永远不通。实测（2026-11-06 CI）：容器 60 秒探活超时，
#   日志里是 `ModuleNotFoundError: No module named 'tiktoken'`。
#
#   它只有几 MB，卸载它省不到什么，代价却是镜像完全不可用。
RUN pip install -r requirements.txt -c requirements.lock \
 && if [ "$INSTALL_DOCLING" = "0" ]; then \
      pip uninstall -y docling docling-core transformers || true; \
    fi

# 再拷贝代码（改代码只让这几层失效）
COPY src ./src
COPY app ./app
COPY scripts ./scripts

# 构建期自检：镜像必须能导入"服务启动必经的那条链"。
#
# 为什么在**构建期**验而不是等 CI 探活（这条是 2026-11-06 的真实事故）：
# 探活失败的表现是"容器 60 秒没起来"，真因埋在 `docker logs` 里，排查要先绕一圈；
# 构建期的报错直接指到缺哪个包。
#
# ⚠️ 必须放在 `COPY src/app` **之后** —— 导入的是本仓库的代码，代码没拷进来时
# `import app.main` 必然 ModuleNotFoundError，那是位置错、不是依赖错。
# 也刻意导入 `app.main` 而不只是 `import tiktoken`：它才是真正的启动路径，
# 缺任何一环都在这里炸，而不是在部署后。
#
# ⚠️ 还得给一个**构建期假 Key**：`app.main` 导入期会走 `_assert_agents_ready()`，
# 没配 `LLM_API_KEY` 就 `missing_api_key` 直接抛 RuntimeError。那本来是对的
# （"能起来但答不出来"不该被放行），但它在构建期没有意义 —— 构建时不该有真凭据，
# 凭据是**运行时**通过 `--env-file` / 编排注入的。所以这里只给一个明显是假的值，
# 让"依赖是否齐全"这件事能被单独验证出来。
#
# 注意：这一步只验**导入**，不会发任何网络请求（构造 client 不需要连上游）。
RUN LLM_API_KEY=sk-build-time-placeholder-not-a-real-key \
    DB_URL= \
    python -c "import app.main" \
 && python -c "import tiktoken, langchain_openai; print('[build] INSTALL_DOCLING=$INSTALL_DOCLING 依赖自检通过')"

# 非 root 运行：容器被攻破时的最小影响面
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# 存活探针走 /health/live —— **刻意不探依赖**：探针失败会触发容器重启，
# 不能因为数据库抖一下就把所有副本重启（那是把故障放大）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:8000/health/live || exit 1

# workers 由 `app.serve.resolve_workers()` 解析：APP_WORKERS → WEB_CONCURRENCY → 1。
#
# **刻意不在 CMD 里写 `--workers`**：命令行参数的优先级高于环境变量，一旦写死
# （历史上是 `--workers ${WEB_CONCURRENCY:-2}`），运维在 .env / compose 里设的
# `APP_WORKERS` 就会被**静默覆盖**——"配了 4 个 worker 实际只起 1 个"，且日志无痕。
#
# 默认 1 而不是 2：多 worker 会各自持有检索器与限流计数，**没配 REDIS_URL 时
# 实际额度会翻倍**。要上多 worker 就显式设 APP_WORKERS/WEB_CONCURRENCY 并配 REDIS_URL。
#
# 走 `python -m app.serve` 而不是裸 uvicorn：它会注入自定义事件循环工厂
# （见 app/serve.py）。Linux 上行为与裸 uvicorn 等价（Selector/uvloop），
# 同一份镜像拿到 Windows 上跑也不会因 ProactorEventLoop 丢掉检查点持久化。
CMD ["python", "-m", "app.serve", "--host", "0.0.0.0", "--port", "8000"]
