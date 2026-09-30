# chatAgent —— 任务入口（给 CI / Linux / WSL 用的一层薄转发）
# =============================================================================
#
# ⚠️ 真正的逻辑不在这里，在 `scripts/tasks.py`（纯标准库、跨平台）。
#    原因：`make` 在 Windows 上**默认不存在**（Git for Windows 不带），而本项目
#    的主力开发环境就是 Windows —— 只写 Makefile 的话，本地只能靠记忆敲长命令，
#    而"记不住就跳过"是所有运维动作衰减的起点。
#
#    Windows 上直接用：
#       python scripts/tasks.py check
#       python scripts/tasks.py drill
#
#    这里每个目标都是一行转发，不重复任何逻辑。两边是否一一对应，由
#    `tests/test_tasks_entrypoint.py` 钉住（加了目标却忘了加任务，或反过来，
#    都会在那条守卫里红）。
#
# 用法
# ----
#     make            # = make help
#     make check      # 提交前门禁（与 CI 的 test job 等价）
#     make test ARGS="-- -k version"
#     make up
#     make drill

PYTHON ?= python
TASKS  := scripts/tasks.py

# 透传给被调用工具的参数：make test ARGS="-- -k version"
ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help setup test test-fast check version sync-version openapi lock \
        backup-pg backup-qdrant drill canary e2e up up-observability down ps logs \
        release rollback clean

help:
	@$(PYTHON) $(TASKS) help

setup:
	@$(PYTHON) $(TASKS) setup

test:
	@$(PYTHON) $(TASKS) test $(ARGS)

test-fast:
	@$(PYTHON) $(TASKS) test-fast $(ARGS)

check:
	@$(PYTHON) $(TASKS) check

version:
	@$(PYTHON) $(TASKS) version

sync-version:
	@$(PYTHON) $(TASKS) sync-version

openapi:
	@$(PYTHON) $(TASKS) openapi

lock:
	@$(PYTHON) $(TASKS) lock

backup-pg:
	@$(PYTHON) $(TASKS) backup-pg

backup-qdrant:
	@$(PYTHON) $(TASKS) backup-qdrant

drill:
	@$(PYTHON) $(TASKS) drill

canary:
	@$(PYTHON) $(TASKS) canary $(ARGS)

e2e:
	@$(PYTHON) $(TASKS) e2e $(ARGS)

up:
	@$(PYTHON) $(TASKS) up

up-observability:
	@$(PYTHON) $(TASKS) up-observability

down:
	@$(PYTHON) $(TASKS) down

ps:
	@$(PYTHON) $(TASKS) ps

logs:
	@$(PYTHON) $(TASKS) logs $(ARGS)

release:
	@$(PYTHON) $(TASKS) release $(ARGS)

rollback:
	@$(PYTHON) $(TASKS) rollback $(ARGS)

clean:
	@$(PYTHON) $(TASKS) clean $(ARGS)
