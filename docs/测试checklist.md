# 测试 Checklist（测试经理）

## 已执行并通过（在 DgxSpark ARM64 服务器真实环境，2026-06-27）

| 序号 | 测试内容 | 测试依据 | 测试目的 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `core.env_doctor.run_all_checks()` 四项检测 | Project_Task.md 2.3.2 | 验证环境自愈检测在真实服务器上识别正确 | ✅ cuda_compat/网卡(1000Mb/s)/drop_caches/swap 均 ok |
| 2 | `cli.main model_check` 扫描真实模型目录 | Project_Task.md 2.3.3 模型选择 | 验证模型扫描器对真实33个模型的格式/量化/引擎/上下文识别正确性 | ✅ 28通用+5嵌入，全部分类正确 |
| 3 | Web后端登录鉴权 | 技术方案.md 方案A | 验证JWT签发/校验、401行为 | ✅ 缺token返回401，登录成功返回有效token |
| 4 | `core.config.load_config` 幂等性 | 内部bug | 配置文件不存在时是否重复生成不同密钥 | ✅ 修复前❌曾失败，修复后首次加载即落盘，重复一致 |
| 5 | GET /api/overview 聚合接口 | 2.3.3 实时总览 | 验证env+resources+models+components一次性聚合返回 | ✅ 返回真实GPU功耗/内存/env状态 |
| 6 | 危险操作确认门禁(env/fix/swap) | 2.3.2 安全设计 | 未带confirmed=true时是否拒绝执行 | ✅ 返回400并提示需确认 |
| 7 | 前端 `npm run build` 类型检查 | 技术方案.md 方案A | Vue3+TS代码在Node18环境下能否正确构建 | ✅ vue-tsc无报错，9个页面chunk全部生成 |
| 8 | 前后端联调(vite preview + uvicorn同时运行) | 阶段三验收 | 前端静态资源与后端API是否能同机并行对外服务 | ✅ 均返回200 |
| 9 | SearXNG容器真实拉起 | 2.3.3 高级设置 / API发布 | `docker compose -f deploy/searxng-compose.yml up -d`能否成功 | ✅ 容器Up，`/search?format=json`返回真实结果 |
| 10 | `/api/searxng/status` `/api/searxng/search` | 阶段四验收 | 后端封装的SearXNG调试接口 | ✅ 真实数据 |
| 11 | **核心功能：模型加载全链路** | 2.3.3 模型加载 | 通过Web API触发→docker compose渲染→vllm容器启动→权重加载→OpenAI兼容API推理→卸载 | ✅ 完整闭环跑通（见下方bug记录） |

## 发现并修复的Bug

| Bug | 严重程度 | 根因 | 修复 | 验证方式 |
| --- | --- | --- | --- | --- |
| `load_config()` 文件不存在时每次调用返回不同随机密钥/密码 | 严重（登录必然失败） | 未在首次加载时落盘 | 改为首次加载即`save_config` | 重启服务两次确认admin_password一致 |
| vllm compose command 缺少 `vllm serve` 可执行文件 | 严重（模型加载必然失败） | 容器entrypoint是裸`exec "$@"`，无默认CMD | command列表前置`["vllm","serve"]` | 真实容器加载7B模型成功 |
| compose command 缺少 `--model` 路径参数 | 严重 | render_compose只拼了`--served-model-name`，未传模型路径 | 增加`--model {容器内路径}` | 同上 |
| `--flag value` 合并为单个字符串token | 严重（YAML list每项应为独立argv） | 拼接成`"--gpu-memory-utilization 0.5"`一个字符串 | 拆分为`"--gpu-memory-utilization"`, `"0.5"`两项 | 同上 |

## 阶段五新增验证（2026-06-27，真实GPU环境）

| 序号 | 测试内容 | 结果 |
| --- | --- | --- |
| 12 | ollama容器化加载（Modelfile + `docker exec ollama create`） | ✅ 真实加载15GB Q8 gguf模型，`ollama list`确认注册成功 |
| 13 | ollama真实推理 | ✅ `/api/chat`返回真实生成内容，15 t/s |
| 14 | ollama卸载（`ollama rm`） | ✅ 卸载后`ollama list`确认已清空 |
| 15 | API调试转发 - OpenAI格式 | ✅ 完整请求包/响应包正确返回，真实命中vllm `/v1/chat/completions` |
| 16 | API调试转发 - Claude格式转换 | ✅ 响应体正确转换为`content`/`stop_reason`/`usage`结构 |
| 17 | 性能测试模块（4并发请求） | ✅ 全部成功，吞吐量计算正确(34.28 tok/s)，报告落盘 |
| 18 | `/api/perf/reports` 历史报告列表 | ✅ 正确返回 |
| 19 | `/api/metrics/history` 指标历史查询 | ✅ 10秒间隔的真实CPU/内存/GPU/功耗数据 |

## 本轮发现并修复的Bug（阶段五）

| Bug | 严重程度 | 根因 | 修复 |
| --- | --- | --- | --- |
| 模型加载引擎选择默认硬编码`vllm`，不看模型实际格式 | 严重（gguf模型加载必然崩溃，报出令人困惑的pydantic校验错误而非"应该用ollama"提示） | `ModelLoadRequest.engine`默认值未结合`model.engine_hint`校正 | `models_router.load_model`改为始终信任`model_scanner`检测到的`engine_hint`，忽略/校正客户端传错的engine字段 |

## 阶段六：全面查缺补漏（2026-06-27，真实环境）

| 序号 | 测试内容 | 结果 |
| --- | --- | --- |
| 20 | 嵌入模型真实加载（Qwen3-VL-Embedding-2B） | ✅ 真实生成向量，`/v1/embeddings`返回正确维度的embedding |
| 21 | 通用+嵌入模型同时加载的端口分配 | ✅ 修复后默认 8001(通用)/8002(嵌入)，不再冲突 |
| 22 | 高级设置 GET/PUT /api/settings | ✅ 密钥/密码脱敏返回；PUT真实落盘到settings.yaml并立即生效 |
| 23 | 组件日志 - 已加载模型 | ✅ 真实容器日志内容正确返回 |
| 24 | 组件日志 - SearXNG | ✅ 修复后真实返回searxng容器日志（此前缺failas该组件未注册在registry里） |
| 25 | WebSocket /ws metrics 推送 | ✅ 用python websockets客户端验证，10秒间隔收到2条真实指标快照，鉴权(token query param)生效 |
| 26 | 多分片GGUF模型ollama加载（Gemma-4-31B-JANG_4M-CRACK-GGUF，9分片） | ⚠️→✅ ollama报错`split GGUF "...00001-of-00009.gguf" has 1 shards, expected 9`，确认是ollama工具本身不支持通过单Modelfile FROM指针自动合并分片，非代码bug。服务器上无官方`llama-gguf-split`二进制（`which`/全盘`find`/ollama镜像内查找均未找到），改用Python `gguf`包(`GGUFReader`/`GGUFWriter`)编写`tools/gguf_merge_shards.py`自定义合并脚本：校验`split.tensors.count`元数据与实际张量总数一致(833=833)、拷贝shard0除split.*以外的全部43个KV元数据、按分片顺序拼接全部张量写出单文件。已在真实环境完整跑通：9个分片→合并出30.38GiB单文件→`ollama create`成功解析为单GGUF→`ollama list`显示32GB模型→`/api/generate`真实推理返回结果。合并耗时约1分钟(本地磁盘IO，非GPU瓶颈)。 |

## 本轮(阶段六)发现并修复的Bug

| Bug | 严重程度 | 根因 | 修复 |
| --- | --- | --- | --- |
| 模型加载引擎默认硬编码vllm（已在阶段五记录，详见上方） | — | — | — |
| **`--task embed`在当前vllm版本(0.21+)不存在，会导致embedding模型加载必然崩溃** | 严重 | vllm 0.21+移除了旧的`--task`参数，改用`--convert`+`--runner` | `param_advisor.py`/`docker_helper.py`改用`--convert embed`+`--runner pooling`，已用真实embedding模型验证生成向量成功 |
| 通用模型与嵌入模型同时加载默认都用端口8001 | 中等（仅在同时加载两类模型时触发） | `host_port`默认值未区分`is_embedding` | 嵌入模型默认改为8002 |
| 组件日志接口无法查看SearXNG日志 | 低 | SearXNG未注册在`registry`里，日志路由只认registry | 新增`searxng`别名分支，直连`SearxngManager.logs()` |

## 阶段七：需求缺口全面修复（2026-06-27，真实环境）

经全面核对Project_Task.md发现5个需求缺口，逐一修复并真机验证：

| 序号 | 缺口 | 修复方式 | 验证结果 |
| --- | --- | --- | --- |
| 27 | CLI start/stop/restart/clean 纯占位 | 新增`core/process_manager.py`，真实管理web后端进程(pidfile)+SearXNG+ollama共享容器+扫描`data/compose/*.compose.yml`逐一启停 | ✅ start拉起全部真实组件；stop停止不删容器；clean彻底清理但保留模型扫描结果/性能报告/配置文件 |
| 28 | 环境异常修复方式与需求"提示CLI"不符 | 保留Web一键修复，旁加CLI命令提示文案，两种路径并存 | ✅ 产品体验折中方案 |
| 29 | "API发布"看板完全缺失 | 新增`/api/api-directory`，从registry读取真实加载状态拼出Web后台/通用模型/嵌入模型/SearXNG四个真实可用的接口地址 | ✅ 真实返回（依赖下方#31状态对账修复才能显示真实加载的模型） |
| 30 | 组件内存使用恒为空 + 运行观测KV Cache/启动参数等占位 | `docker stats`批量采集内存；新增`/api/models/{name}/runtime-stats`读取真实compose command + vllm `/metrics` | ✅ 内存/启动参数/运行中请求数/等待请求数/KV Cache使用率全部真实数据 |
| 31 | SearXNG代理配置不持久化 | `AppConfig.searxng_proxy_url`新增字段，PUT落盘，`searxng_client.search()`真实使用该代理 | ✅ 设置假代理后搜索请求确实尝试走该代理并报连接失败（证明生效），清空后恢复正常搜索 |

## 修复过程中追加发现并修复的Bug（不在原定5项范围内，验证时暴露）

| Bug | 严重程度 | 根因 | 修复 |
| --- | --- | --- | --- |
| **registry纯内存态，CLI启动的模型Web完全看不到** | 严重 | web进程重启或模型由CLI而非Web API启动时，`web.state.registry`为空，导致overview/api-directory/components全部显示"未加载"，与CLI/Web"共享同一套状态"的设计初衷相悖 | 新增`web.state.reconcile_from_disk()`，FastAPI启动时通过`docker inspect`真实容器的`--served-model-name`参数重建registry（而非凭文件名猜测） |
| 多个失败/被替换的vllm加载尝试在`data/compose/`堆积同容器名的过期文件，导致状态对账时模型名称与实际运行容器张冠李戴 | 中高 | `gb10-vllm-general`/`gb10-vllm-embedding`是固定共享容器名，旧compose文件从不清理 | 加载新模型前自动删除同容器名的旧compose文件；对账逻辑改为读取容器真实启动参数而非按文件名匹配 |
| `_parse_mem_usage`单位解析永远失败 | 中 | 单位字典遍历顺序`{"B":...,"KiB":...,"MiB":...,"GiB":...,"TiB":...}`，"GiB"等都以"B"结尾，被"B"规则误匹配截断成非法数字 | 改为按单位字符串长度从长到短匹配 |
| vllm `/metrics`的KV Cache指标名猜测错误(`gpu_cache_usage_perc`) | 低 | 未在真机确认就先猜测命名 | 用真实curl输出确认正确指标名为`vllm:kv_cache_usage_perc` |
| SearXNG代理无法通过PUT传`null`清空 | 低 | 更新逻辑统一跳过None值，无法区分"未提供"和"显式清空" | 为`searxng_proxy_url`单独放行None值落盘 |

## 已知遗留发现（已随ollama移除而失效，记录留档）
- ~~`ollama list`显示模型为空~~：2026-06-28 起项目已完全移除ollama引擎（GGUF模型改走vllm加载），此发现不再适用。

## 已确认的架构限制（非bug，不在本次修复范围）

- 性能测试模块未对接MLPerf/LLMPerf等国际标准benchmark（Project_Task.md要求，已在研发方案.md阶段五说明为后续可选集成项）
- 高并发/多用户登录场景未测试（非本项目重点，内部工具用户量低）

## 多分片GGUF合并工具（2026-06-28新增）

- `tools/gguf_merge_shards.py`：用法 `python3 tools/gguf_merge_shards.py <第1个分片路径> <输出文件路径>`，自动按文件名规律发现并校验剩余分片是否齐全。
- 已用真机`Gemma-4-31B-JANG_4M-CRACK-GGUF`(9分片，约30.4GiB张量数据)完整验证：合并出单文件后成功。
- 服务器上确认没有官方`llama-gguf-split`二进制（`which`、全盘`find`均未命中），故采用Python `gguf`包(pip可装)的`GGUFReader`/`GGUFWriter`原语自实现合并逻辑，逻辑参考了该包自带的`gguf_new_metadata.py`脚本的元数据拷贝模式。

## 阶段八：GGUF引擎统一改造 + 页面调整（2026-06-28，真实环境验证）

按用户最新指令完成以下工作，全部在真机ARM64+GPU环境验证：

| 序号 | 改动 | 验证方式 | 结果 |
| --- | --- | --- | --- |
| 32 | **完全移除ollama引擎**：删除`core/docker_helper.OllamaManager`、`param_advisor.recommend_ollama_params`、`process_manager`/`models_router`/`web/state`中全部ollama分支；`model_scanner.engine_hint`的gguf分支改为`"vllm"` | `docker stop/rm gb10-ollama`+删除遗留`ollama.compose.yml`；导入测试`IMPORTS_OK` | ✅ 容器已清理，所有模块正常导入 |
| 33 | **GGUF模型改走vllm加载**：`DockerComposeManager.render_compose()`新增对gguf模型的处理——`--model`指向实际.gguf文件（而非目录）、强制`--dtype float16`（vllm对GGUF不支持bfloat16，已在日志中确认`GGUF has precision issues with bfloat16 on Blackwell`警告）、多分片/不兼容量化类型直接拒绝并报错 | 真实加载`Qwen3-14B-BaronLLM-v2-Q8`(15GB GGUF)：`vllm serve --model .../qwen3-14b...gguf --dtype float16`容器启动成功，`/v1/completions`真实返回"Paris."等正确补全 | ✅ 端到端验证通过 |
| 34 | **GGUF与vllm兼容性检测**：`model_scanner.py`新增`gguf_multi_shard`/`gguf_vllm_compatible`字段，用`gguf.GGUFReader`读取每个张量的GGML量化类型，比对vllm 0.21 GGUF加载层实际支持的类型集合(UNQUANTIZED/STANDARD/KQUANT/IMATRIX) | 扫描全部5个本地GGUF模型 | ✅ 4个兼容(GLM-4.7-Flash/Gemma-31B合并版/Gemma-4-E4B/Qwen3-14B)，1个不兼容：`GPTOSS-120B-...-MXFP4.gguf`含108个MXFP4张量，vllm此版本GGUF加载器不支持MXFP4，已在`model_scanner`/`docker_helper`/`models_router`三层加拒绝校验 |
| 35 | **vllm `--api-key`接入**：新增`AppConfig.vllm_api_key`(格式`sk-xxxx`，明文存储)，所有vllm容器统一加载时带上该key，OpenAI/Claude兼容接口均需Bearer认证 | 真实curl测试：正确key返回200推理结果，错误key返回401 | ✅ 鉴权生效 |
| 36 | **API发布页IP显示修复**：原硬编码`127.0.0.1`改为通过UDP socket trick检测真实局域网出口IP | 真实curl `/api/api-directory`返回`base_url`含`192.168.199.4`而非`127.0.0.1` | ✅ 修复确认 |
| 37 | **API发布页拆分OpenAI/Claude接口条目**：vllm 0.21原生支持`/v1/messages`(Claude格式)，新增独立条目展示该路径+`x-api-key`认证方式 | 真实查看`vllm serve`启动日志路由列表确认`/v1/messages`存在 | ✅ |
| 38 | **总览页组件状态列出固定关键组件**：原先组件状态表为空(仅显示已加载模型)，改为始终显示Web后端/前端/SearXNG/通用模型容器/嵌入模型容器5个固定行，状态来自`docker inspect`/进程pid实际探测 | 真实`GET /api/components`返回5个组件含真实运行状态和内存占用 | ✅ |
| 39 | **max_model_len改为下拉选项**：固定12档(4k~256k)，通用模型默认64k、嵌入模型默认4k | 真实`GET /api/models/{name}/params`确认`max_model_len.default=65536`且`options`含12个档位 | ✅ |
| 40 | **tool_call_parser联动**：`enable_auto_tool_choice`勾选时前端动态启用`tool_call_parser`下拉(此前编辑权限为静态值)，选项来自`vllm serve --help=Frontend`真实读取的39个parser名称，按模型架构猜测默认值(如qwen3→qwen3_xml) | 真实`GET /params`确认`Qwen3-14B`模型猜出`tool_call_parser=qwen3_xml` | ✅ |
| 41 | **API调试(工具)页显示支持的parser名称**：新增`GET /api/models/tool-call-parsers`只读接口，前端工具调用JSON输入框下方展示完整39个名称标签 | `npm run build`类型检查通过 | ✅ |

**修复过程中发现的衍生问题（非新bug，记录留档）**：`cli restart`一次性重启所有组件时，Web后端启动顺序早于模型容器重新拉起，导致`web.state.reconcile_from_disk()`对账时模型容器尚未就绪，组件状态表短暂显示该模型槽位为"运行中但未关联模型名"，待模型容器完全启动后状态会自动纠正（不影响实际推理可用性，仅展示延迟）。该问题在ollama移除之前已存在，不是本轮改动引入。
- 合并出的单文件需要约等同原模型大小的额外磁盘空间（此次约30GB），建议合并前用`df -h`确认空间充足。

## 阶段九～阶段十二：v1.1.1 ~ v1.5.0 联机验证汇总（2026-06-28，真实 ARM64+GPU 环境）

> 以下均在 DgxSpark 服务器真机 curl/docker/WS 客户端验证；详细版本说明见 Task_Tracking.md。

| 序号 | 测试内容 | 版本 | 结果 |
| --- | --- | --- | --- |
| 42 | `/api/overview` 等接口随机超时根因(每请求重扫全部GGUF) | v1.1.1 | ✅ 改为按(路径,mtime,size)缓存GGUF兼容性，二次调用30s→0.03s |
| 43 | 组件日志按容器名/`web`/`frontend`/`searxng`别名查询 | v1.1.1 | ✅ 直连 docker logs，不再依赖registry |
| 44 | quantization 参数自适应（不传 --quantization，vllm 自动从 config.json 检测） | v1.1.2 | ✅ Gemma-4-26B-A4B-NVFP4 自动识别 modelopt 并加载推理成功 |
| 45 | vllm_api_key 持久化（修复每次 load_config 重新随机生成→401） | v1.1.2 | ✅ 两次 GET /api/settings key 一致，Bearer 推理 200 |
| 46 | favicon.ico 生成与服务 | v1.1.3 | ✅ GET /favicon.ico 返回 200 image/x-icon |
| 47 | 全部 27 个 /api/* 接口审查 + `/api/components` 批量 docker inspect 优化 | v1.1.4 | ✅ 逐一 200；补全 settings 的 frontend_port 字段 |
| 48 | 单模型加载限制（通用/嵌入各 1 个） | v1.2.0 | ✅ 已加载 2B 再加载 8B 返回 409+中文提示；重载同名 200 |
| 49 | API 健康主动检测 `/api/api-directory/health-check` | v1.2.0 | ✅ 真实探测各端点；抓出嵌入容器旧 key 不匹配并经重载修复 |
| 50 | 版本发布脚本 `tools/release.sh` | v1.2.0 | ✅ 自身用它发布；版本号校验+tag+追加版本表 |
| 51 | CLI 非 venv 启动 web 后端（优先用 .venv/bin/python） | v1.2.0 | ✅ 裸 `python3 -m cli.main restart` 后端正常起 |
| 52 | API 调试转发缺 Authorization 头致 401 | v1.2.1 | ✅ 转发自动带 Bearer+x-api-key，复现的 401 用例转 200 全包返回 |
| 53 | 组件状态内存 MB 标注 + SearXNG 启停重启 | v1.2.1 | ✅ SearXNG restart 动作真机成功 |
| 54 | API 调试页重设计（常用参数前移 + 支持接口表 + system 字段说明） | v1.2.1 | ✅ 构建通过 |
| 55 | 组件状态显示绑定 IP（0.0.0.0 局域网 / 127.0.0.1 仅本机） | v1.3.0 | ✅ 各组件 bind_host=0.0.0.0 |
| 56 | 健康检测名称错配修复 + Claude 鉴权提示纠正（vllm /v1/messages 用 Bearer 非 x-api-key） | v1.3.0 | ✅ 名称对齐无"未检测"；/v1/messages Bearer 200、x-api-key 401（直连） |
| 57 | 组件日志改名"嵌入式模型容器" + 下拉带端口标注 | v1.3.0 | ✅ |
| 58 | SearXNG 禁用 brave 引擎（限流刷错误日志） | v1.3.0 | ✅ 重启后搜索仍 200，brave 报错归零 |
| 59 | 运行观测量化显示（未量化模型显示 torch_dtype） | v1.3.0 | ✅ Qwen3-VL-Embedding-2B 显示"未量化（bfloat16）" |
| 60 | API 调试 Embedding 测试方法 + 通用模型验证简化 | v1.3.0 | ✅ /api/debug/embedding 返回 2048 维向量 |
| 61 | 高频接口迁移 WebSocket（overview/api_directory/runtime_stats 推送） | v1.3.0 | ✅ WS 客户端确认 3 个主题到达 |
| 62 | **模型文件完整性校验**：缺分片/无权重/GGUF不兼容 | v1.4.0/v1.4.1 | ✅ 合成测试覆盖缺分片/完整/截断三态；真机 33 模型仅 GPTOSS-120B(MXFP4) 判无效，无误报 |
| 63 | 无效模型前端高亮 + 加载 422 门禁 + CLI 标红 | v1.4.0 | ✅ 加载缺分片模型返回 422+原因 |
| 64 | **OpenAI+Claude 统一对外网关**(`/v1/*` 端口8000) | v1.4.0 | ✅ /v1/models(200)、无key(401)、/v1/embeddings(200)、/v1/messages 用 x-api-key(200)、chat completions 正常 JSON |
| 65 | 多维度校验升级（index.json weight_map 权威清单 + total_size 大小核对）+ 可选 SHA256 深度校验 | v1.4.1 | ✅ 合成测试三态正确；`model_check --verify-hash` 无清单时优雅跳过 |
| 66 | **模型扫描改手动+持久化缓存**：/api/models/rescan + 前端按钮 + 启动只load不scan | v1.5.0 | ✅ 缓存读 4ms（原全量扫描），手动 rescan 32s 仅显式触发，scanned_at 正确 |
| 67 | GPU 采集优化：无 WS 客户端连接时跳过重型页面快照广播 | v1.5.0 | ✅ 空闲时不再跑 docker stats 等重型采集 |

## GPU / nvidia-smi 资源占用审计（v1.5.0）

- 全代码**未调用 `nvidia-smi` 子进程**；GPU 指标只在 `web/background_tasks.py:_read_gpu()` 一处，
  用 **pynvml(NVML 进程内绑定)** 读取利用率/温度/功耗，每 10s 一次，NVML 初始化一次后每次仅 3 个
  C-API 调用，开销极低（远低于 nvidia-smi 每次 fork 子进程的方式，本项目已是更优方案）。
- 周期性开销的真正大头是 `docker stats`/`docker inspect`（组件与运行观测）与（v1.5.0 前的）模型扫描；
  已通过「无 WS 客户端时跳过页面快照广播」+「扫描改手动缓存」两项显著降低空闲资源消耗。

## 阶段十三：v1.6.0 ~ v1.8.0 联机验证汇总（2026-06-28/29，真实 ARM64+GPU 环境）

| 序号 | 测试内容 | 版本 | 结果 |
| --- | --- | --- | --- |
| 68 | 性能测试 401（perf 转发补 vllm_api_key Bearer） | v1.6.0 | ✅ 4/4 成功、吞吐 70.38 tok/s |
| 69 | 运行观测模型级 tok/s（vllm /metrics 计数器差分） | v1.6.0 | ✅ 基线 0，生成后 prefill=4.9/decode=44.9 tok/s |
| 70 | 高级设置 SearXNG 检索 URL 用真实 host+port | v1.6.0 | ✅ 前端构建通过，按 window.location.hostname 拼接 |
| 71 | 性能测试历史记录删除（单条/清空全部+路径穿越防护） | v1.6.1 | ✅ 单删 6→5、不存在 404、穿越尝试安全拒绝 |
| 72 | SearXNG 链接指向 localhost 修复（base_url:false 按请求 Host 推导） | v1.6.2 | ✅ opensearch.xml 用 192.168.199.4 / 跟随 100.x，localhost 计数 0 |
| 73 | Claude CLI /v1/messages 报 400 system role 修复（网关清洗 system 入顶层） | v1.6.3 | ✅ 非流式 200、流式完整 Anthropic SSE 事件序列 |
| 74 | 性能测试实时进度（WS perf_progress）+失败请求记录请求/响应包 | v1.7.0 | ✅ 进度 start→1/5…→done；错误捕获 status=401+请求4字段+响应体 |
| 75 | API 调试页 Temperature 场景下拉 + System/Prompt 默认示例 | v1.8.0 | ✅ 构建通过 |
| 76 | 组件日志/模型配置页日志改 WebSocket 推送（set_log_target + logs 主题） | v1.8.0 | ✅ WS 收到 gb10-vllm-general 日志推送（2340 字节） |
| 77 | 对外 /v1/* OpenAI+Claude REST 保持不变 | v1.8.0 | ✅ chat/messages(x-api-key)/debug 均 200 |
| 78 | 全部只读接口冒烟（overview/models/components/env/settings/api-directory/health-check/metrics/perf/searxng/tool-call-parsers） | v1.8.0 | ✅ 全部 200 |

## 前端通信方式现状（v1.8.0）

- **WebSocket 推送**（topic）：`metrics`（系统资源）、`overview`（总览整页）、`api_directory`（API发布表）、
  `runtime_stats`（运行观测/tok-s）、`perf_progress`（性能测试进度）、`load_progress`（加载进度）、
  `logs`（组件/模型日志，按 `set_log_target` 点对点推送）。已无 setInterval 轮询。
- **REST（一次性，用户操作/页面首屏）**：登录、模型列表/参数/加载/卸载/重扫、调试 chat/embedding、
  环境修复、组件启停、性能测试运行/报告/删除、SearXNG 启停/搜索/代理、设置读写、指标历史首屏。
- **REST（对外数据面，刻意保留）**：`/v1/*` OpenAI 与 Claude 兼容网关（外部程序/Claude CLI 调用）。

## 阶段十四：v1.9.0 ~ v2.0.0 联机验证（2026-06-29，真实环境）

| 序号 | 测试内容 | 版本 | 结果 |
| --- | --- | --- | --- |
| 79 | 模型级 tok/s 改持久化趋势图（model_tps 写入 metrics_history，经 /api/metrics/history 返回） | v1.9.0 | ✅ 通用模型 decode 入库；历史含两模型曲线（补 schema/router 后 with_model_tps=55） |
| 80 | tok/s 单一差分源（runtime_stats 读 LATEST_TPS，不再双重差分） | v1.9.0 | ✅ runtime-stats 与历史一致 |
| 81 | 高级设置 SearXNG 服务地址显示局域网可访问地址（hostname+port） | v2.0.0 | ✅ 不再固定 127.0.0.1 |
| 82 | 移动端/iPad 响应式：宽表横向滚动、导航横滑、内边距收窄（≤900/≤640 媒体查询） | v2.0.0 | ✅ 构建通过 |
| 83 | 模型级 tok/s 显示峰值（最大值）+ 趋势图；修复 Sparkline 含 NaN 时路径失效的 bug | v2.0.0 | ✅ 历史 decode 峰值 42.5 tok/s 可算出并展示 |
| 84 | 回归冒烟（overview/models/components/settings/metrics/api-directory/health-check/perf/searxng + 网关 /v1/models） | v2.0.0 | ✅ 全部 200 |

## 阶段十五：vllm 容器升级 26.05.post1→26.06 + cuda-compat-13-3（2026-06-30，真实环境）

| 序号 | 测试内容 | 版本 | 结果 |
| --- | --- | --- | --- |
| 85 | 新镜像 `nvcr.io/nvidia/vllm:26.06-py3` 内 vllm 版本 | v2.1.0 | ✅ `import vllm`→0.22.1（旧 26.05.post1 为 0.21.0） |
| 86 | 新镜像内 CUDA 版本与宿主 cuda-compat 匹配 | v2.1.0 | ✅ 容器 `nvcc` = 13.3，对应 `cuda-compat-13-3`（宿主 `/usr/local/cuda-13.3/compat` 已存在含 libcuda.so/.1） |
| 87 | cuda-compat 注入端到端：新镜像 + `LD_LIBRARY_PATH=/usr/local/cuda/compat` + 只读挂载 `/usr/local/cuda-13.3/compat`→容器 `/usr/local/cuda/compat` | v2.1.0 | ✅ 容器内 `torch.cuda.is_available()=True`，device 0 = NVIDIA GB10（torch 2.13） |
| 88 | param_advisor 选项随 0.22.1 真机参数更新 | v2.1.0 | ✅ 对照 `vllm serve --help`/QUANTIZATION_METHODS：tool-call-parser 增 `apertus`、quantization 增 `auto_gptq`、kv-cache-dtype 增 `nvfp4`、tokenizer-mode 增 `hf`；convert/runner 选项不变 |
| 89 | `--task` 移除仍成立、`/metrics` 指标名仍存在 | v2.1.0 | ✅ 0.22.1 仍无 `--task`（用 `--convert`+`--runner`）；grep 容器 vllm 源码确认 `num_requests_running`/`num_requests_waiting`/`kv_cache_usage_perc` 三个指标名仍在 |
| 90 | GGUF 支持类型集合在 0.22.1 未变 | v2.1.0 | ✅ 读 `gguf.py` 的 UNQUANTIZED/STANDARD/KQUANT/IMATRIX 集合与 `VLLM_SUPPORTED_GGUF_TYPES` 完全一致，MXFP4 GGUF 仍不支持 |
| 91 | 改动模块导入冒烟 | v2.1.0 | ✅ core.{config,env_doctor,docker_helper,param_advisor,model_scanner} 全部导入正常，默认值已切到 26.06-py3 / cuda-13.3 |

## 阶段十六：26.06 推理500根因修复 + 加载失败检测 + 模型审计增强（2026-06-30，真实环境）

| 序号 | 测试内容 | 版本 | 结果 |
| --- | --- | --- | --- |
| 92 | 复现 /v1/* 全部 500 根因 | v2.2.2 | ✅ 真机确认：26.06 内 fastapi0.137.1 的 `_IncludedRouter` 无 `.path`，prometheus_fastapi_instrumentator8.0.0 中间件读 `route.path` 抛 AttributeError；/metrics(excluded)200 而 chat/embeddings 500 |
| 93 | 补丁镜像修复验证 | v2.2.2 | ✅ `gb10-vllm:26.06-py3-patched`(sed 改 getattr 容错) 上：embedding 200(2048维)、general chat 200(30B真实生成)、网关8000 chat/models 200 |
| 94 | cuda-compat-13-3 注入在补丁镜像仍正常 | v2.2.2 | ✅ 容器启动日志 "CUDA Forward Compatibility mode ENABLED. Using CUDA 13.3"，torch 见 GB10 |
| 95 | #1 加载失败检测-显示层 | v2.3.0 | ✅ 组件状态对已登记但容器 exited/dead 的槽位改报 `failed`+原因（此前硬编码 running）；真机 `docker stop` 嵌入容器后 /api/components 立即显示 `failed:容器已退出(exited)…` |
| 96 | #1 加载失败检测-加载时 | v2.3.0 | ✅ load 端点 `docker compose up` 后短轮询，容器秒退即返回 accepted=false + 日志错误摘要，不再误登记为已加载（helper 真机对 exited 容器返回失败摘要） |
| 97 | #4 tokenizer 完整性新增校验 | v2.3.0 | ✅ safetensors 缺词表文件(tokenizer.json/*.model/vocab.*)判为无效；GGUF 不误报（自带分词器）；全量33模型重扫仅1个无效(GPTOSS-120B MXFP4，真问题非误报) |
| 98 | #4 模型审计端点 `GET /api/models/{name}/audit` | v2.3.0 | ✅ 返回 config/tokenizer/shard 完整性、架构/类型(model_type)/量化/上下文/权重大小/vLLM支持/推荐启动参数(max_model_len/seqs/batched_tokens/gpu_mem)/静态校验结论/错误摘要；真机对 30B 模型字段全部正确 |
| 99 | 确认 vllm 0.22.1 原生支持范围 | v2.3.1 | ✅ openapi.json：OpenAI(/v1/chat/completions,/v1/completions,/v1/models,/v1/responses,嵌入模型上/v1/embeddings) + Claude(/v1/messages,/v1/messages/count_tokens) 均原生；真机 /v1/messages→200 |
| 100 | 去除 debug_router 的 Claude↔OpenAI 同质翻译层 | v2.3.1 | ✅ api_format=claude 改为直连原生 /v1/messages 返回真实响应(不再伪造 _source 信封)；真机 url=/v1/messages、type=message、无 _source；openai 格式回归 /v1/chat/completions 200 |
| 101 | Anthropic system 清洗逻辑收敛为共享模块 | v2.3.1 | ✅ 新增 web/anthropic_compat.py，gateway 与 debug 共用；网关 /v1/messages 传 system 角色消息仍 200（原生直传会 400）；x-api-key 仅网关接受(原生 401) |
| 107 | 扫描/审计补充模型辅助信息(MoE/参数量/结构/类型) | v2.3.5 | ✅ scanner 新增 is_moe/num_experts/num_experts_per_tok(按 num_experts·n_routed_experts·num_local_experts + experts_per_tok/moe_topk,兼顾 text_config 嵌套)、param_count(优先 safetensors index 的 total_parameters,否则求和张量头;量化模型标注为打包计数近似)、hidden_size/num_hidden_layers;audit 增「参数量/是否MoE/专家总数/激活专家/隐藏维度/层数」行;ModelInfoOut 同步字段。真机:Qwen3-VL=MoE128/激活8·约18B·hidden2048·48层、Coder=MoE512/10·约45B、Gemma=Dense·约20.9B,均正确 |
| 108 | OpenWebUI tool_choice=auto 报错修复(param_advisor 默认启用工具) | v2.3.5 | ✅ 根因:客户端默认发 tool_choice=auto,vllm 未带 --enable-auto-tool-choice/--tool-call-parser 启动即硬报错。改为 tool-capable 且能推断解析器的模型默认 enable_auto_tool_choice=True(嵌入/pooling 模型 gate 关闭避免启动失败)。真机:tool_choice=auto+tools(纯文本)=200、+图片(OpenWebUI 实际场景)=200 并正确分析图片 |
| 111 | 加载失败后页面不再误显示"已加载/卸载" | v2.3.6 | ✅ 根因:/api/overview 与 /api/models 的 load_status 仅取内存 registry(只记录"已发起加载"),容器崩溃后仍显示已加载。新增 state.real_load_status() 用真实 docker State.Status 解析 registry 模型为 running/failed;overview 拆出 general/embedding_models_failed,models 列表 load_status 崩溃→error;前端总览失败模型显示红色「加载失败」+「清理」按钮(非卸载),模型页状态列中文化(已加载/加载失败/未加载)。真机复现:嵌入模型 gpu_mem=0.9 与通用共存 OOM 崩溃→overview embedding_models_failed=[该模型]、models load_status=error(修复前显示已加载) |
| 119 | max_model_len / max_num_batched_tokens 下拉以「k」显示 | v2.3.14 | ✅ 前端新增 optionLabel(key,opt):仅对 max_model_len/max_num_batched_tokens 两个上下文档位,把整千位的 token 数换算成 k 显示(65536→64k、524288→512k、1048576→1024k);下拉 value 仍是原始 token 数,提交不变。其它下拉(kv_cache_dtype/dtype/max_num_seqs 等)保持原样(max_num_seqs=2 仍显示 2,fp8 仍显示 fp8)。基础/高级两处 <option> 均已应用;node 校验+前端 build 通过 |
| 118 | 上下文档位扩展到 512k/768k/1024k(支持 1M 上下文模型) | v2.3.13 | ✅ 现象:Qwythos-9B-Claude-Mythos-5-1M 支持 1,048,576 上下文,但 max_num_batched_tokens/max_model_len 下拉最高只到 256k。根因:CONTEXT_LEN_OPTIONS 档位表封顶 262144。修复:表内新增 524288/786432/1048576(512k/768k/1024k);两参数本就是 <select>(有 options),各档按模型 max_position_embeddings 自动上限(_context_len_options)。真机:1M 模型 /params 两参数下拉出现 512k/768k/1024k;256k 模型仍封顶 256k(不误增) |
| 117 | 模型输出满屏"!"排障 + 新增 enforce_eager 稳定性选项 | v2.3.12 | ✅ 现象:Qwen3.6-35B NVFP4 接口输出连续 '!' 且请求卡住。诊断:'!'=logits NaN(采样退化到 token0/'!');/metrics 显示 2 请求 running 但 kv_cache 0.55% 不动=引擎 wedged。根因是该 aggressive NVFP4 合并量化偶发数值异常致引擎卡死(非网关/参数)。处置:重载清除卡死后真机验证短/数学/~2k长输入均正常生成(非'!')。新增可选 enforce_eager 参数(param_advisor+docker_helper bool_flag_map→--enforce-eager,vllm0.22.1 有效),关 CUDA graph 规避重放数值异常;默认关,高级参数里可勾选。真机:参数暴露+渲染正确,模型健康 200 |
| 116 | 运行观测「模型级 tok/s」四图排版与显示修复 | v2.3.11 | ✅ 问题:最近才加载的模型(如通用35B仅~60min数据)在整天窗口里被挤到最右~5%、左侧大片空白;4个图平铺一行又矮又挤;嵌入模型 Decode 恒0 图无意义。修复:①tpsSeries 按模型「首次出现数据」下标裁剪(真机 general 7488→365点/~60min铺满宽,embedding 不变),prefill/decode 共起点对齐;②改为按模型分组卡片(模型名只出现一次,prefill+decode 并排),图更高(96px);③嵌入模型不再画空的 Decode 图,改为文字说明「无自回归 decode」;④补充说明 prefill 天然脉冲、横轴按加载后跨度。前端 build 通过 |
| 115 | 网关流式转发 TransferEncodingError 修复(瞬时上游断连) | v2.3.10 | ✅ 现象:OpenWebUI chat 报 'Response payload is not completed: TransferEncodingError: Not enough data to satisfy transfer length header'。根因:web.log 见 gateway event_stream 抛 httpx.ReadError/RemoteProtocolError('Server disconnected without sending a response')——打开到 vllm 的流时瞬时断连;原流式路径无重试无异常处理,生成器抛异常→chunked SSE 未发终止块→客户端 aiohttp 报 TransferEncodingError。直连:8001 流式正常、gateway 短请求正常(非必现)。修复:gateway 流式打开重试3次(仅在未发出任何字节时安全重试),重试耗尽或中途失败则以 error 事件+[DONE] 干净终止 SSE(客户端收到错误而非截断体)。真机:happy path 82 chunk+[DONE]+0 error,重试逻辑不影响正常流 |
| 114 | 图片分析报"No space left on device"修复(/dev/shm 64MB→16GB) | v2.3.9 | ✅ 现象:OpenWebUI 上传图片分析报 No space left on device,纯文本 chat 正常;宿主磁盘 1.9T 空闲(非真磁盘满)。根因:docker 默认 /dev/shm=64MB,vllm/torch 处理多模态图片张量写共享内存瞬间撑爆;compose 模板未设 shm_size。修复:COMPOSE_TEMPLATE 加 shm_size:"16gb"(tmpfs 按需占用非预留)。真机:重建容器后 df /dev/shm=16G(原 64M);需重载既有模型才生效 |
| 113 | vllm serve 改用位置参数传模型(去 --model 废弃告警) | v2.3.8 | ✅ 当前容器 vllm 0.22.1;真机启动日志确认告警"With vllm serve, you should provide the model as a positional argument … The --model option will be removed in a future version"。render_compose 由 `vllm serve --model <path>` 改为 `vllm serve <path>`(位置参数 model_tag)。重扫全部 emit 的旗标,真机启动日志中仅 --model 有废弃告警(其余 gpu-memory-utilization/max-model-len/kv-cache-dtype/convert/runner/tool-call-parser 等均正常,无告警);另有 transformers 内部 Qwen2VLImageProcessorFast/use_fast 告警属 vllm 内部非本项目旗标。修复后真机重载:日志出现 model_tag 位置参数、无 --model 告警、模型正常加载 |
| 112 | gpu_memory_utilization 可用上限提醒 | v2.3.6/v2.3.7 | ✅ /params 新增 gpu_memory_hint{mem_free_gb,mem_total_gb,suggested_max};通用/嵌入配置页 gpu_memory_utilization 字段下方文字提醒"当前空闲X/总Y GiB,建议≤Z(仅提醒非强制)"。v2.3.7 校准:pynvml GPU 显存在 GB10 Not Supported,psutil free 比 vllm 实际用的 CUDA free 高约 3-4GiB(context 开销),故 suggested_max=floor((free-4GiB)/total) 保守预留,避免"照提示填 0.2 仍 OOM"。真机:free27.24/total121.69→0.19(校准前 0.21) |
| 110 | 通用/嵌入模型页详细展示模型分析结果 | v2.3.5 | ✅ ModelInfoOut+api.ts 补 model_type 及 is_moe/num_experts/num_experts_per_tok/param_count/hidden_size/num_hidden_layers;GeneralModelView(通用/嵌入两页共用)选中模型后新增「模型分析」卡:哪种模型(model_type+架构)、参数量、是否MoE(专家总/激活)、是否支持图片、多模态、工具调用、量化、最大上下文、隐藏维度/层数、格式大小、文件校验。真机 /api/models(general+embedding) 均返回全部字段;前端 build 通过 |
| 109 | 工具解析器按 chat 模板格式选择(修 name=None) | v2.3.5 | ✅ 二次报错 function.name=None 根因:qwen3_xml 解析器只适配 Qwen3-Coder 的 XML `<function=>` 格式,而 Qwen3-VL 用 Hermes `<tool_call>\n{json}\n</tool_call>` 格式→误解析出 name=None,回传历史时 400。改 _guess_tool_call_parser 优先读 chat_template.jinja:含 `<function=`→qwen3_xml,含 `<tool_call>`→hermes,再回退架构名。验证:Qwen3-VL→hermes、Coder→qwen3_xml;重载 compose 含 --tool-call-parser hermes;工具调用 name 有效 + 多轮回传 round-trip 200 |
| 105 | 多模态能力检测(scanner/audit/list/UI) | v2.3.4 | ✅ model_scanner 新增 multimodal/modalities 检测(image=vision_config/图像处理器;video=video_preprocessor/video_config;audio=audio_config;不靠裸 token_id 避免 Gemma 误报 video/audio);audit 增「是否支持多模态/图片分析/工具功能」行;模型列表+API+通用模型页新增「多模态」列。真机重扫 33 模型:Qwen3-VL=图片+视频、Gemma-4=仅图片、Coder=纯文本,分类正确 |
| 106 | Qwen3-VL-30B-A3B-Thinking-NVFP4 图片分析排障 | v2.3.4 | ✅ 结论:模型+网关+OpenAI/Claude 两种格式对图片分析全部 200 且正确识别(实测 336~2688px,2688px≈9216tok 超过 batched 8192 仍成功→chunked-prefill 正常,非 token 限制)。真正病因是加载参数:gpu_memory_utilization=0.9 与嵌入模型共存时 OOM(free 91<desired 109 GiB)导致容器启动即崩→API 报错;降到 0.6 两模型共存,图片分析正常。另:远程 http 图片 URL vLLM 默认不抓取,需用 base64 |
| 104 | 运行观测 tok/s 图「峰值」重复显示修复 | v2.3.3 | ✅ 根因:模型级图表既在 .chart-value 头部渲染 `峰值 {{tpsPeak()}}`,又给 Sparkline 传 `:show-peak=true`(组件内部再渲染一次 `峰值 {{peak}}`,同一序列同值)→峰值显示两次。移除头部冗余 span + 死代码 tpsPeak()/.chart-peak CSS,保留组件 show-peak(v2.0.0 为此新增)。与系统级图表布局对齐;npm build 通过,ObservabilityView bundle 由 10.90→10.63kB |
| 103 | 运行观测 tok/s 趋势图非连续修复（Sparkline 跨 gap 桥接） | v2.3.2 | ✅ 根因:嵌入模型 prefill 序列多为 idle-0,其"缺失"来自模型未加载/未采样的 tick(全天 8728 tick 中 embedding 缺席 1018),Sparkline 遇 null/NaN 断成多段(v2.0.0 的分段行为)→视觉"非连续"。改为跳过 gap 但不抬笔,下一有效点用 L 桥接(x 用原始索引,水平跨度正确)。node 模拟:代表性带 gap 序列由 3 段断线→1 条连续线;前端 npm build 通过 |
| 102 | vllm 补丁镜像缺失自愈 `ensure_vllm_image()` | v2.3.1 | ✅ 加载/`cli start` 前检查 config.vllm_image 是否本地存在；缺失且=补丁tag→自动 docker build deploy/vllm-patch.Dockerfile(幂等,真机用 throwaway tag 验证 build 命令 0.2s 缓存通过)；缺失且非补丁tag→返回明确错误(不再让 docker compose 去 pull 本地tag报错);已存在→秒过no-op。单测三分支(存在/非补丁缺失/build命令)全过 |
