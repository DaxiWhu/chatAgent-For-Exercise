"""PG 容量采集脚本的守卫（2026-10-03 新增）。

为什么值得单独一个文件：这个脚本的产物本身就是**监控的输入**，而它坏掉的方式
恰好是最难发现的一种 —— 文件还在、内容也"看起来对"，但 Prometheus 读不到任何东西
（一行语法错误会让 textfile collector **丢掉整个文件**的指标）。
于是所有容量告警**静默失效**：没有序列可算 ≠ 报 0，规则根本匹配不上。

所以这里钉的是**字节级的事实**（行尾、转义、失败后文件还在不在），
而不是"某个函数被调用过"。
"""

from __future__ import annotations

import json

import pytest

from scripts import pg_table_stats as st


# --------------------------------------------------------------------------- #
# 1. 渲染：产物必须能被 Prometheus 读回去
# --------------------------------------------------------------------------- #
def test_render_produces_parseable_prometheus_text():
    text = st.render_prom([("agent_messages", 1234, 56)], 9999)

    assert 'pg_table_size_bytes{table="agent_messages"} 1234' in text
    assert 'pg_table_rows_estimate{table="agent_messages"} 56' in text
    assert "pg_database_size_bytes 9999" in text
    # 末尾换行：没有的话，下一个写这个文件的人会把内容粘到最后一行上
    assert text.endswith("\n")
    # HELP 与 TYPE 成对
    assert text.count("# TYPE pg_table_size_bytes gauge") == 1
    assert text.count("# TYPE pg_table_rows_estimate gauge") == 1
    assert text.count("# TYPE pg_database_size_bytes gauge") == 1


def test_label_values_are_escaped():
    """表名里的反斜杠与引号必须转义。

    漏转义 ⇒ 某一行语法错误 ⇒ textfile collector **丢掉整个文件**的指标
    （不是只丢那一行）⇒ 容量在监控里彻底消失。而表名是 DBA 建的，
    不是本脚本能假定"一定干净"的输入。
    """
    text = st.render_prom([('we"ird\\name', 1, 1)], 1)

    assert 'pg_table_size_bytes{table="we\\"ird\\\\name"} 1' in text
    line = next(ln for ln in text.splitlines() if ln.startswith("pg_table_size_bytes{"))
    # 裸引号只允许出现两处（开标签值与闭合）；其余必须带反斜杠。
    assert line.count('\\"') == 1, "标签值里的引号没有转义 —— 这一行会被提前截断"
    assert line.count("\\\\") == 1, "反斜杠没有转义"


def test_render_sorts_nothing_but_keeps_every_table():
    """表的顺序由 SQL 决定，渲染层不重排 —— 但一张都不能漏。

    漏表 ⇒ 那张表的序列消失 ⇒ `delta()` 无值可算 ⇒ **增长告警对它是瞎的**。
    """
    tables = [(f"t{i}", i * 10, i) for i in range(5)]
    text = st.render_prom(tables, 100)
    for name, size, _rows in tables:
        assert f'table="{name}"}} {size}' in text


# --------------------------------------------------------------------------- #
# 2. 写盘：字节级纪律
# --------------------------------------------------------------------------- #
def test_write_uses_lf_even_on_windows(tmp_path):
    """指标文件必须是 LF 行尾 —— 这条在 Windows 上写代码时特别容易踩。

    `open(..., "w")` 的默认换行转换会把 `\\n` 变成 `\\r\\n`，而 Prometheus 的文本
    格式不接受裸 `\\r`。表现是**本机手工跑出来的文件是坏的、容器里跑出来的却是好的**
    —— "只在某一边坏"的那类缺陷。所以这里钉**磁盘上的字节**，不钉函数行为。
    """
    out = tmp_path / "pg-tables.prom"
    st.write_prom_atomically("a 1\nb 2\n", str(out))

    raw = out.read_bytes()
    assert b"\r\n" not in raw, "写成了 CRLF —— Prometheus 会读不到这些指标"
    assert raw.endswith(b"\n")


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    """先写临时文件再替换：抓取端随时可能在读，半截文件会让它丢掉整个文件。"""
    out = tmp_path / "pg-tables.prom"
    st.write_prom_atomically("x 1\n", str(out))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "pg-tables.prom"]
    assert leftovers == [], f"留下了临时文件：{leftovers}"


# --------------------------------------------------------------------------- #
# 3. 失败路径：绝不能让指标消失
# --------------------------------------------------------------------------- #
def test_failed_collection_keeps_the_previous_file(monkeypatch, tmp_path):
    """采集失败时**不动**上一版文件。

    指标一旦消失，`delta()` / `deriv()` 就没有序列可算 ⇒ **静默不告警** ——
    而"容量看不见"正是这个脚本要消灭的东西。所以宁可用一份过期数据。
    """
    out = tmp_path / "pg-tables.prom"
    out.write_text("pg_database_size_bytes 42\n", encoding="utf-8")

    def boom(_dsn):
        raise RuntimeError("PG 连不上")

    monkeypatch.setattr(st, "fetch_stats", boom)
    with pytest.raises(RuntimeError):
        st.collect("postgresql://x", str(out))

    assert out.read_text(encoding="utf-8") == "pg_database_size_bytes 42\n", (
        "采集失败却把旧指标覆盖/删掉了 —— 容量序列会断裂"
    )


def test_missing_db_url_exits_with_its_own_code(monkeypatch):
    """没配 DB_URL ⇒ 退出码 2 且原因明确。

    与 `src/agents/session.py` 同一条理由：让下游抛一句读起来像"驱动装错了"的话，
    会把排查带到完全错误的方向。
    """
    monkeypatch.setattr(st.config, "DB_URL", "")
    assert st.main([]) == 2


# --------------------------------------------------------------------------- #
# 4. 成功路径与 --json
# --------------------------------------------------------------------------- #
def test_collect_writes_the_metrics_file(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "fetch_stats", lambda _d: ([("t", 5, 1)], 7))
    out = tmp_path / "out.prom"

    result = st.collect("postgresql://x", str(out))

    assert result["tables"] == 1
    text = out.read_text(encoding="utf-8")
    assert "pg_database_size_bytes 7" in text
    assert 'pg_table_size_bytes{table="t"} 5' in text


def test_json_mode_reports_the_same_facts(monkeypatch, capsys):
    monkeypatch.setattr(st.config, "DB_URL", "postgresql://x")
    monkeypatch.setattr(st, "fetch_stats", lambda _d: ([("agent_messages", 10, 2)], 99))

    assert st.main(["--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["database_size_bytes"] == 99
    assert data["tables"][0] == {
        "table": "agent_messages",
        "size_bytes": 10,
        "rows_estimate": 2,
    }
