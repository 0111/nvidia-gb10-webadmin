# API 清单

> NVIDIA GB10 Manager 全部对外/对内接口清单（对应 v1.8.x）。
> 基地址：管理与数据面同在 `http://<服务器IP>:8000`（默认 `http://192.168.199.4:8000`）。
> 路由来源：运行实例的 `GET /openapi.json` + `web/routers/*` + `web/ws_router.py`。

## 鉴权方式说明

| 标记 | 含义 |
| --- | --- |
| 公开 | 无需鉴权 |
| JWT | 管理前端用：先 `POST /api/auth/login` 取 `access_token`，置于请求头 `Authorization: Bearer <token>` |
| API-Key | 对外数据面用：`vllm_api_key`（`sk-xxxx`，见高级设置/settings）。`Authorization: Bearer <key>` 或 `x-api-key: <key>` 均可 |

---

## 一、鉴权 / 健康

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 1 | POST | `/api/auth/login` | 公开 | 管理后台登录，签发 JWT | body：`username`(str)、`password`(str)。返回 `access_token`/`token_type`/`expires_in_seconds` |
| 2 | GET | `/api/health` | 公开 | 存活探针 | 无。返回 `{"status":"ok"}` |

## 二、实时总览 / 组件 / 环境

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 3 | GET | `/api/overview` | JWT | 总览聚合：模型加载状态 + 环境检查 + 系统资源 + 组件状态 | 无。（同等数据也经 WS `overview` 主题每 10s 推送） |
| 4 | GET | `/api/components` | JWT | 关键组件状态（Web后端/前端/SearXNG/通用容器/嵌入容器）：容器名、端口+绑定IP、内存(MB)、状态、是否可操作 | 无 |
| 5 | POST | `/api/components/{name}/{action}` | JWT | 对组件执行操作（当前 SearXNG 与已加载模型可控） | path：`name`(组件名/SearXNG)、`action`(`start`/`stop`/`restart`) |
| 6 | GET | `/api/env/checklist` | JWT | 环境检测：cuda-compat / 网卡协商 / drop_caches / swap | 无 |
| 7 | POST | `/api/env/fix/{check_name}` | JWT | 对某项环境检查执行修复（危险操作需确认） | path：`check_name`；body：`confirmed`(bool，必须 true 才执行) |

## 三、模型管理

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 8 | GET | `/api/models` | JWT | 列出本地模型（来自缓存的扫描结果，含校验 valid/validation_errors、量化、dtype、上下文上限、是否已加载、scanned_at） | query：`type`(可选 `general`/`embedding`) |
| 9 | POST | `/api/models/rescan` | JWT | 手动触发一次磁盘扫描+多维度校验并落盘缓存（唯一会真正读文件的操作） | 无。返回 `{total, invalid, general, embedding, scanned_at}` |
| 10 | GET | `/api/models/tool-call-parsers` | JWT | 返回当前 vLLM 支持的 `tool_call_parser` 名称列表（39 个） | 无 |
| 11 | GET | `/api/models/{name}/params` | JWT | 该模型的 vLLM 启动参数智能推荐（默认值/可选项/来源说明） | path：`name`(模型名) |
| 11b | GET | `/api/models/{name}/audit` | JWT | 模型完整性/就绪审计：config/tokenizer/shard 完整性、架构/类型/量化/上下文、权重大小、vLLM 是否支持、推荐启动参数、静态校验结论与错误摘要（返回 `fields` 扁平表 + `rows` 有序标签列表） | path：`name`(模型名) |
| 12 | POST | `/api/models/{name}/load` | JWT | 加载模型（渲染 compose 并 `docker compose up -d`）。校验未过→422；同类已加载→409 | path：`name`；body：`engine`("vllm")、`params`(参数字典，如 served_model_name/max_model_len/gpu_memory_utilization 等)、`host_port`(可选) |
| 13 | POST | `/api/models/{name}/unload` | JWT | 卸载模型（`docker compose down`），释放显存 | path：`name`(已加载的模型名) |
| 14 | GET | `/api/models/{name}/runtime-stats` | JWT | 该模型运行时指标：启动参数、内存、运行中/等待请求数、KV Cache 使用率、prefill/decode tok/s、累计 tokens | path：`name`。（同等数据也经 WS `runtime_stats` 推送） |

## 四、API 调试

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 15 | POST | `/api/debug/chat` | JWT | 向已加载通用模型转发一次对话调试请求，返回完整请求包/响应包（自动带 vllm_api_key） | body：`model_name`、`api_format`(`openai`/`claude`)、`system`(可选)、`prompt`、`max_tokens`、`temperature`、`extra`(可选，如 tools) |
| 16 | POST | `/api/debug/embedding` | JWT | 向已加载嵌入模型转发一次向量化调试请求（响应中向量摘要为维度+前若干值） | body：`model_name`、`input`(待向量化文本) |

## 五、组件日志

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 17 | GET | `/api/logs/{component}` | JWT | 拉取组件日志（一次性）。前端实时查看已改用 WS `logs` 主题 | path：`component`(`web`/`frontend`/`searxng`/任意 `gb10-` 容器名/已加载模型名)；query：`lines`(默认 200) |

## 六、性能测试

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 18 | POST | `/api/perf/run` | JWT | 对已加载模型做并发吞吐压测；进度经 WS `perf_progress` 推送；失败请求记录请求/响应包 | body：`model_name`、`concurrency`(默认4)、`num_requests`(默认8)、`prompt`、`max_tokens`(默认256)、`temperature`(默认0.7)、`stream`(默认false) |
| 19 | GET | `/api/perf/reports` | JWT | 历史测试报告列表 | 无 |
| 20 | GET | `/api/perf/reports/{report_id}` | JWT | 单份测试报告详情（含每请求结果与失败详情） | path：`report_id` |
| 21 | DELETE | `/api/perf/reports/{report_id}` | JWT | 删除单份测试报告（含路径穿越防护） | path：`report_id` |
| 22 | DELETE | `/api/perf/reports` | JWT | 清空全部测试报告 | 无。返回 `{deleted}` |

## 七、运行观测（指标历史）

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 23 | GET | `/api/metrics/history` | JWT | 系统指标历史（内存/GPU负载/温度/功耗），供趋势图 | query：`window`(如 `1d`)。实时值经 WS `metrics` 推送 |

## 八、SearXNG 本地搜索

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 24 | GET | `/api/searxng/status` | JWT | SearXNG 容器运行状态 | 无 |
| 25 | POST | `/api/searxng/start` | JWT | 启动 SearXNG 容器 | 无 |
| 26 | POST | `/api/searxng/stop` | JWT | 停止 SearXNG 容器 | 无 |
| 27 | GET | `/api/searxng/search` | JWT | 经后端（含持久化代理）执行一次 SearXNG JSON 搜索 | query：`q`(查询词) |
| 28 | POST | `/api/searxng/proxy/test` | JWT | 测试到 SearXNG 的（可选）网络代理连通性 | body：`proxy`(代理URL，可空=测直连) |

## 九、设置

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 29 | GET | `/api/settings` | JWT | 读取配置（端口/host/管理员账号密码/secret_key/vllm_api_key/模型目录/SearXNG/代理等，均明文） | 无 |
| 30 | PUT | `/api/settings` | JWT | 更新配置并落盘生效 | body：可选字段子集（如 `searxng_proxy_url`、`web_port`、`frontend_port`、`searxng_port`/`url` 等） |

## 十、API 发布看板

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 31 | GET | `/api/api-directory` | JWT | 列出对外可用接口（网关 OpenAI/Claude/Embedding + 管理后台 + 直连容器 + SearXNG）的名称/用途/BaseURL/密钥 | 无 |
| 32 | GET | `/api/api-directory/health-check` | JWT | 主动探测上述各接口健康状态 | 无。返回 `{overall_healthy, results[]}` |

## 十一、对外数据面网关（OpenAI / Claude 兼容，端口 8000）

> 统一入口 `http://<服务器IP>:8000/v1`，按请求体 `model` 字段路由到已加载容器（找不到回退到唯一已加载的通用/嵌入模型）。**鉴权用 API-Key**（Bearer 或 x-api-key 均可）。支持流式 `"stream":true`。

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 33 | GET | `/v1/models` | API-Key | （OpenAI）列出当前可服务的模型 | 无 |
| 34 | POST | `/v1/chat/completions` | API-Key | （OpenAI）对话补全 | body：`model`、`messages[]`(role/content)、`max_tokens`、`temperature`、`stream`、`tools` 等 |
| 35 | POST | `/v1/completions` | API-Key | （OpenAI）文本补全 | body：`model`、`prompt`、`max_tokens`、`temperature` 等 |
| 36 | POST | `/v1/embeddings` | API-Key | （OpenAI）文本向量化（路由到嵌入模型） | body：`model`、`input`(字符串或数组) |
| 37 | POST | `/v1/messages` | API-Key | （Claude/Anthropic）Messages 对话。网关自动清洗：把混入 messages 的 system 角色并入顶层 `system` | body：`model`、`max_tokens`、`system`(顶层)、`messages[]`、`stream` 等。头可加 `anthropic-version` |
| 38 | POST | `/v1/messages/count_tokens` | API-Key | （Claude）统计消息 token 数（同样清洗 system） | body 结构同 `/v1/messages`，无需 max_tokens |

## 十二、WebSocket（实时推送）

| 序列 | 方法 | 路径 | 鉴权 | 功能描述 | 参数描述 |
| --- | --- | --- | --- | --- | --- |
| 39 | WS | `/ws` | JWT(查询参数) | 单一连接多主题推送通道 | 连接：`/ws?token=<JWT>&topics=<逗号分隔主题>`。运行中可发消息：`{"action":"subscribe","topics":[...]}` 改订阅；`{"action":"set_log_target","component":<标识>,"lines":N}` 订阅某组件日志（component=null 停止） |

**WS 主题（topic）一览**：

| 主题 | 内容 | 频率 |
| --- | --- | --- |
| `metrics` | 系统资源快照（CPU/内存/GPU负载/温度/功耗） | 每 10s |
| `overview` | 实时总览整页快照 | 每 10s |
| `api_directory` | API 发布表 | 每 10s |
| `runtime_stats` | `{模型名: 运行指标}`（含 tok/s） | 每 10s |
| `perf_progress` | 性能测试进度 `{completed,total,failed,stage}` | 每请求完成时 |
| `load_progress` | 模型加载/卸载阶段进度 | 状态变化时 |
| `logs` | `{component, content}`，按 `set_log_target` 点对点推送 | 每 ~10s |

> 注：所有 `metrics`/`overview`/`api_directory`/`runtime_stats`/`logs` 在「无 WS 客户端连接」时不采集不推送，以降低空闲资源消耗。
