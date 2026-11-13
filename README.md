# 六爻纳甲 · AI 解卦

本地可运行的六爻排盘与 AI 解卦 Web 系统。前端为原生 JavaScript 单页，后端为 FastAPI。排盘全部是确定性计算，AI 只负责解读，不参与起卦。

## 功能

- **铜钱摇卦**：三枚铜钱逐爻摇动，六次成卦后自动排盘
- **手动排爻**：逐爻点选少阳 / 少阴 / 老阳 / 老阴，选满自动排盘，可一键清空
- **手动指定卦象**：按八宫分组的可搜索卦名选择器，已有卦象时直接排盘
- **确定性排盘**：六神、纳甲、六亲、世应、神煞、动爻取用、爻间五行生克、合冲刑害破
- **天机时间罗盘**：四柱、农历、旬空，支持指定历史起卦时间
- **经典排盘表**：仿主流六爻排盘软件的分列展示，含动爻与世应摘要
- **AI 解卦**：OpenAI 兼容接口，内置 11 条断卦规则，按四段式输出
- **流式输出**：解卦走 SSE 逐段上屏，可中途停止，60 秒无数据自动断开

## 目录结构

```
├─ main.py                   后端服务（FastAPI，单文件）
├─ static/index.html         前端单页（HTML + CSS + JS）
├─ hexagrams.json            六十四卦基础数据 + 爻辞
├─ jiao_yilin.json           焦氏易林林辞
├─ requirements.txt          依赖清单
├─ .env.example              环境变量模板
└─ README.md
```

## 快速启动

要求 Python 3.10+。

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env，填入 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL
python main.py
```

启动后访问：

- 首页：http://127.0.0.1:8000/
- 接口文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health

不配置 `.env` 时，排盘功能仍可正常使用，AI 解卦会提示未配置。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_BASE_URL` | 空 | OpenAI 兼容接口地址 |
| `OPENAI_API_KEY` | 空 | 接口密钥，不填则 AI 解卦不可用 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 模型名 |
| `APP_HOST` | `127.0.0.1` | 监听地址，默认只允许本机访问 |
| `APP_PORT` | `8000` | 监听端口 |
| `MAX_BODY_BYTES` | `1048576` | 单次请求体上限，超限返回 413 |
| `RATE_LIMIT_PER_MINUTE` | `10` | 单 IP 每分钟解卦次数，0 关闭限流，超限返回 429 |
| `ADMIN_TOKEN` | 空 | 携带该令牌请求 `/api/health` 才会返回模型与限额详情 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/paipan` | 按本卦/变卦卦码排盘，动爻 = 两卦码逐爻差异 |
| POST | `/api/divine` | AI 解卦，一次性返回完整断语 |
| POST | `/api/divine/stream` | AI 解卦，SSE 流式：`meta` → `delta`* → `done` / `error` |
| GET | `/api/hexagrams` | 六十四卦知识库（带 ETag，未变更返回 304） |
| GET | `/api/health` | 健康检查与配置状态 |

`/api/paipan` 请求示例：

```json
{ "ben_gua_code": "101010", "bian_gua_code": "100110", "qigua_time": "2026-11-04T20:30" }
```

- 卦码为 6 位 `0/1`，**自下而上**，前 3 位为下卦、后 3 位为上卦，`1` 阳 `0` 阴。
- `qigua_time` 可省略，缺省取服务器当前时间。

## 数据说明

- `hexagrams.json`：按 6 位卦码索引的六十四卦数据，含名称、上下卦、卦宫、五行、世爻位置、卦序、卦辞，以及 `yao_ci` 字段下的六条爻辞（初爻 → 上爻）。
- `jiao_yilin.json`：按「本卦之变卦」短名索引的焦氏易林林辞。

以上文本均为传统典籍内容，仅作学习与研究使用。

## 安全提示

- `.env` 含 API Key，已被 `.gitignore` 排除，请勿提交。
- `/api/divine` 不信任前端传入的排盘结果：卦码、月建、日辰、旬空、纳甲、六亲、六神、神煞一律在服务端按卦码与起卦时间重算。
- 上游 AI 报错只记录在服务端日志，对外统一返回通用文案，不泄露模型名、地址或上游响应。
- 内置请求体上限（413）与单 IP 滑动窗口限流（429 + `Retry-After`），默认监听 `127.0.0.1`。
- 如需公网部署：改为 `0.0.0.0` 前请加反向代理、鉴权与 HTTPS；限流状态保存在进程内，多实例部署需换成 Redis 等共享存储。

## 免责声明

本项目用于传统文化学习与娱乐，解卦内容由大模型生成，不构成任何决策、医疗、法律或投资建议。
