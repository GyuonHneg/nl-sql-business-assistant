"""NL-SQL Business Assistant —— 自然语言查询业务库并返回表格 + 图表。

用法:
    python assistant.py --mode tui     # 终端交互
    python assistant.py --mode gui     # WebUI

环境变量:
    DASHSCOPE_API_KEY   DashScope API Key
    DATABASE_URL        只读库连接串，如 mysql+pymysql://user:pass@host:3306/db
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import dashscope
import numpy as np
import pandas as pd
from qwen_agent.agents import Assistant
from qwen_agent.tools.base import BaseTool, register_tool
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("nl-sql-assistant")

# ---------------------------------------------------------------- 中文字体回退
import matplotlib  # noqa: E402

matplotlib.use("Agg")  # 无显示环境下必须切换后端
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "SimSun", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

# ---------------------------------------------------------------- 安全闸门
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|replace|merge)\b",
    re.IGNORECASE,
)
_MAX_ROWS = int(os.getenv("MAX_ROWS", "5000"))


class UnsafeSQLError(ValueError):
    """SQL 命中写操作关键字，拒绝执行。"""


def assert_readonly(sql: str) -> str:
    """只允许单条 SELECT / WITH 查询。"""
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise UnsafeSQLError("拒绝执行多语句查询")
    if _FORBIDDEN.search(stripped):
        raise UnsafeSQLError(f"只允许只读查询，命中关键字: {_FORBIDDEN.search(stripped).group(1)}")
    if not re.match(r"^\s*(select|with)\b", stripped, re.IGNORECASE):
        raise UnsafeSQLError("只允许 SELECT / WITH 语句")
    return stripped


def get_engine():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("未配置 DATABASE_URL，请在环境变量中设置只读库连接串")
    return create_engine(
        dsn,
        connect_args={"connect_timeout": 10},
        pool_size=int(os.getenv("POOL_SIZE", "10")),
        max_overflow=int(os.getenv("MAX_OVERFLOW", "20")),
        pool_pre_ping=True,
    )


# ---------------------------------------------------------------- 工具实现
@register_tool("exc_sql")
class ExcSQLTool(BaseTool):
    """执行 SQL，返回 Markdown 表格 + 自动生成图表。

    把「执行 / 表格化 / 可视化」合并为一个工具，避免 LLM 漏掉画图这一步。
    """

    description = "执行 SQL 查询，返回 Markdown 表格，并自动生成对应图表"
    parameters = [
        {"name": "sql_input", "type": "string", "description": "生成的 SQL 语句", "required": True}
    ]

    def call(self, params: str, **kwargs) -> str:
        args = json.loads(params)
        sql = assert_readonly(args["sql_input"])
        log.info("执行 SQL: %s", sql)

        df = pd.read_sql(text(sql), get_engine())
        if len(df) > _MAX_ROWS:
            df = df.head(_MAX_ROWS)
            log.warning("结果集超过 %d 行，已截断", _MAX_ROWS)

        md = df.head(10).to_markdown(index=False)
        img_md = render_chart(df)
        return f"{md}\n\n{img_md}"


def _safe(label: str) -> str:
    """转义 matplotlib 会当作格式化字符的符号。"""
    return str(label).replace("%", "%%").replace("{", "{{").replace("}", "}}")


def render_chart(df: pd.DataFrame) -> str:
    """按 dtype 自动选堆积图 / 分组图，返回图片 markdown。"""
    if df.empty or len(df.columns) < 2:
        return "_（结果不足以绘图）_"

    cols = list(df.columns)
    x_col = cols[0]
    obj_cols = [c for c in df.select_dtypes(include="O").columns if c != x_col]
    num_cols = df.select_dtypes(exclude="O").columns.tolist()

    fig, ax = plt.subplots(figsize=(10, 6))

    if obj_cols and num_cols:
        pivot = df.pivot_table(index=x_col, columns=obj_cols, values=num_cols, fill_value=0)
        bottoms = None
        for col in pivot.columns:
            ax.bar(pivot.index, pivot[col], bottom=bottoms, label=_safe(col))
            bottoms = pivot[col].copy() if bottoms is None else bottoms + pivot[col]
    else:
        x = np.arange(len(df))
        bottom = np.zeros(len(df))
        for col in cols[1:]:
            if col not in num_cols:
                continue
            ax.bar(x, df[col], bottom=bottom, label=_safe(col))
            bottom += df[col].values
        ax.set_xticks(x)
        ax.set_xticklabels([_safe(v) for v in df[x_col]], rotation=45)

    ax.set_xlabel(_safe(x_col))
    ax.set_ylabel("数量")
    ax.set_title("查询结果")
    ax.legend()
    fig.tight_layout()

    # 直接输出 base64，避免磁盘堆积
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)  # 必须关闭，否则长跑会内存泄漏
    import base64

    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"![查询结果](data:image/png;base64,{b64})"


# ---------------------------------------------------------------- 助手装配
SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.md").read_text(encoding="utf-8")


def build_bot() -> Assistant:
    dashscope.api_key = os.getenv("DASHSCOPE_API_KEY", "")
    dashscope.timeout = int(os.getenv("LLM_TIMEOUT", "30"))
    if not dashscope.api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")

    return Assistant(
        llm={
            "model": os.getenv("LLM_MODEL", "qwen-turbo"),
            "timeout": int(os.getenv("LLM_TIMEOUT", "30")),
            "retry_count": 3,
        },
        name="业务查询助手",
        description="自然语言查询业务数据",
        system_message=SYSTEM_PROMPT,
        function_list=["exc_sql"],
    )


def run_tui() -> None:
    bot = build_bot()
    messages: list[dict] = []
    print("输入问题开始查询（Ctrl-C 退出）")
    while True:
        try:
            query = input("you: ").strip()
            if not query:
                continue
            messages.append({"role": "user", "content": query})
            reply = []
            for chunk in bot.run(messages):
                reply = chunk
            messages.extend(reply)
            print("bot:", reply[-1].get("content") if reply else "（无返回）")
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            break
        except Exception as exc:  # noqa: BLE001
            log.exception("处理失败")
            print(f"[error] {exc}")


def run_gui() -> None:
    from qwen_agent.gui import WebUI

    bot = build_bot()
    WebUI(
        bot,
        chatbot_config={
            "prompt.suggestions": [
                "最近三个月各类票种的销量是多少？按周统计",
                "上个月各省份的订单量排名",
                "各销售渠道的订单金额占比",
            ]
        },
    ).run()


def main() -> None:
    ap = argparse.ArgumentParser(description="NL-SQL Business Assistant")
    ap.add_argument("--mode", choices=["tui", "gui"], default="tui")
    args = ap.parse_args()
    try:
        run_gui() if args.mode == "gui" else run_tui()
    except Exception as exc:  # noqa: BLE001
        log.exception("启动失败")
        sys.exit(f"启动失败: {exc}")


if __name__ == "__main__":
    main()
