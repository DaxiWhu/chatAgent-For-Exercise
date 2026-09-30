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
RUN pip install -r requirements.txt -c requirements.lock \
 && if [ "$INSTALL_DOCLING" = "0" ]; then \
      pip uninstall -y docling docling-core transformers tiktoken || true; \
    fi

# 再拷贝代码（改代码只让这几层失效）
COPY src ./src
COPY app ./app
COPY scripts ./scripts

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
