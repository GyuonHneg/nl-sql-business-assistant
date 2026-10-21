# NL-SQL Business Assistant

用自然语言查业务数据库：**一句话提问 → LLM 生成 SQL → 执行 → 返回 Markdown 表格 + 自动生成的图表**。

基于 Qwen-Agent + DashScope 的 Function Calling 能力，把「SQL 执行 / 结果表格化 / 自动可视化」封装成一个复合工具，让 LLM 一次调用就拿到「数据 + 图」。

```text
用户："4、5、6 月一日票和二日票的销量多少？按周统计"
        │
        ▼
┌──────────────────────────────────────────────┐
│  Assistant (qwen-turbo)                       │
│  system_prompt 内嵌：表结构 + SKU 清单 + 写法模板 │
└───────────────┬──────────────────────────────┘
                │ Function Call: exc_sql(sql_input)
                ▼
┌──────────────────────────────────────────────┐
│  ExcSQLTool                                   │
│   1. SQL 安全校验（只读 + 白名单）              │
│   2. pandas.read_sql → DataFrame              │
│   3. to_markdown() → 表格                     │
│   4. 自动判类型 → 堆积/分组柱状图 → base64      │
└───────────────┬──────────────────────────────┘
                ▼
         Markdown 表格 + 图表（原样回传给用户）
```

## 为什么把三件事合成一个工具

最初拆成 `exc_sql` / `draw_chart` 两个工具，实测问题很明显：

| 方案 | 问题 |
|---|---|
| 拆成两个工具 | LLM 经常只调 `exc_sql` 就结束，图没画；或画了图但表格丢了 |
| **合成一个工具** | 一次调用同时拿到表格和图，链路从 2 步降到 1 步，成功率明显上升 |

**结论**：工具粒度应按「用户一次想看到什么」来划分，而不是按「技术动作」划分。

## 关键实现

### 1. 复合工具（`@register_tool`）

```python
@register_tool('exc_sql')
class ExcSQLTool(BaseTool):
    description = '执行 SQL 查询，返回 Markdown 表格并自动生成图表'
    parameters = [{'name': 'sql_input', 'type': 'string', 'required': True}]

    def call(self, params: str, **kwargs) -> str:
        sql = json.loads(params)['sql_input']
        assert_readonly(sql)              # 安全闸门
        df = pd.read_sql(text(sql), engine)
        md = df.head(10).to_markdown(index=False)
        img = render_chart(df)            # 自动判类型
        return f"{md}\n\n{img}"
```

### 2. Prompt 里内嵌 schema 与写法模板

把建表语句、SKU 清单、常见聚合写法直接写进 `system_message`，显著降低 LLM 试错：

```sql
-- 一日/二日票的口径直接给模板，避免 LLM 自己猜 LIKE 模式
SUM(CASE WHEN SKU LIKE '%One-Day%' THEN quantity ELSE 0 END) AS one_day_ticket_sales,
SUM(CASE WHEN SKU LIKE '%1.5-Day%' THEN quantity ELSE 0 END) AS two_day_ticket_sales
```

并明确约束输出行为，防止模型"自作主张"总结掉图表：

> 每当 `exc_sql` 返回 markdown 表格和图片时，你必须**原样输出全部内容**（包括图片 markdown），不要只总结表格。

### 3. 图表自动选型

不看配置，直接根据 DataFrame 的 dtype 判断：

- 存在 object 列（除首列外）→ `pivot_table` 做**堆积柱状图**
- 纯数值列 → 多列**分组柱状图**

### 4. 会话隔离

多用户场景下每个会话持有独立结果，避免串扰：

```python
_last_df_dict = {}          # session_id -> DataFrame
def get_session_id(kwargs):
    messages = kwargs.get('messages')
    return id(messages) if messages is not None else None
```

## 安全设计（相对原实现的重点改造）

让 LLM 直接产出并执行 SQL，风险主要在**越权**和**注入**。这里做了三层：

1. **只读账号**：数据库连接使用只读权限账号，从环境变量注入，不硬编码
   ```python
   engine = create_engine(os.environ['DATABASE_URL'])   # 而非写死连接串
   ```
2. **SQL 白名单校验**：拦截 `DROP / DELETE / UPDATE / INSERT / ALTER / TRUNCATE` 等写操作
3. **执行超时**：`connect_timeout=10` + 连接池上限，避免慢查询拖死服务

> 更严格的生产方案是「SQL 白名单模板」——只允许匹配预置查询模板，本项目保留只读校验作为轻量方案。

## 踩过的坑

| 问题 | 现象 | 解决 |
|---|---|---|
| 中文乱码 | 图表标题/坐标轴全是方框 | `plt.rcParams['font.sans-serif']` 按 `SimHei → Microsoft YaHei → SimSun → Arial Unicode MS` 依次回退 |
| 内存泄漏 | 长时间运行后内存持续上涨 | 每次 `savefig` 后必须 `plt.close()` |
| 格式化字符 | 列名含 `%` 时 `plt.bar(label=...)` 报错 | 标签统一 `replace('%','%%').replace('{','{{').replace('}','}}')` |
| 日期格式 | 模型把日期解析错 | 换用 `qwen-turbo` 并显式给出日期口径 |
| 图片堆积 | 磁盘被 `image_show/` 占满 | 图表改为按 TTL 清理 / 直接返回 base64 |

## 效果评估

SQL 是**确定性输出**，所以用离线用例集评估，而不是主观打分：

- 维护一份「问题 → 期望 SQL」的离线用例集
- 关注两个指标：**工具调用成功率**（是否触发 `exc_sql`）与 **SQL 正确率**（执行结果是否匹配期望）
- 图表等主观产物再用 LLM-as-Judge 抽样评分

## 快速开始

```bash
pip install -r requirements.txt
export DASHSCOPE_API_KEY=sk-xxx
export DATABASE_URL="mysql+pymysql://readonly:pass@host:3306/db?charset=utf8mb4"

python assistant.py --mode tui     # 终端交互
python assistant.py --mode gui     # WebUI（Gradio）
```

## 目录结构

```text
.
├── assistant.py       # 入口：CLI / WebUI 双模式
├── tools.py           # exc_sql 复合工具 + 图表渲染
├── evaluator.py       # 离线用例集评估
├── requirements.txt
└── .env.example
```

## 参考

- 原型来自 Qwen-Agent 官方 Function Calling 案例（`assistant_ticket_bot`），本仓库在其基础上做了：连接串环境变量化与脱敏、SQL 只读校验、日志替换 print、会话隔离改造、离线评估脚本。
- 评估方法论参考 LangFuse / OpenEvals：确定性输出走客观指标，主观产物走 LLM-as-Judge。

## License

MIT
