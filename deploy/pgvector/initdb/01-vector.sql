-- 容器首次初始化时执行：启用 pgvector。
-- 镜像里已带编译好的扩展，这一步只是注册，不涉及任何编译。
-- 幂等：即使 PgVectorStore.ensure_schema() 也建过，重复执行无副作用。
CREATE EXTENSION IF NOT EXISTS vector;
