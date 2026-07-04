# deploy/ 自动部署与 SearXNG 集成

本目录包含阶段四交付的部署脚本与 SearXNG 本地搜索引擎的 compose 模板。
所有脚本/compose 文件都在 DGX Spark (ARM64 Linux) 服务器上执行（现已通过
claude + SSH 直接登录该服务器开发与运行）。

## 1. 运行 bootstrap.sh

```bash
cd /home/spark/nvidia-gb10-manager
bash deploy/bootstrap.sh
```

执行流程：

1. **第一步 — 环境 checklist 检测**：复用 `core.env_doctor.run_all_checks()`，
   依次检测 cuda-compat、网卡协商速度、drop_caches、swap 状态，打印每项的
   满足/不满足情况及处理建议命令。检测完成后会通过 `read -p` 提示用户确认
   是否继续。
2. **第二步 — 实际部署**：
   - 创建 Python venv（`.venv/`）
   - `pip install -r requirements.txt`
   - 调用 `core.config.load_config()`，若 `config/settings.yaml` 不存在，
     会自动生成（含随机 `secret_key` / `admin_password`）并写盘；若已存在
     则直接加载，不会覆盖现有配置。
   - 创建 `data/`、`data/compose/`、`data/logs/`、`data/reports/` 目录。

## 2. 启动 SearXNG

```bash
cd /home/spark/nvidia-gb10-manager/deploy
docker compose -f searxng-compose.yml up -d
```

- 默认监听端口 `8080`（可通过环境变量 `SEARXNG_PORT` 覆盖，需与
  `config/settings.yaml` 中的 `searxng_port` 保持一致）。
- `searxng-settings.yml` 以只读方式挂载到容器内 `/etc/searxng/settings.yml`，
  其中 `search.formats` 显式包含 `json`，以支持
  `GET /search?q=<query>&format=json&categories=general&language=auto&safesearch=0`
  这种调用方式（Project_Task.md「API 发布」章节约定的 URL 格式）。
- `server.secret_key` 留了占位字符串，生产部署建议替换为随机值，例如：
  `openssl rand -hex 32`。

停止/查看状态：

```bash
docker compose -f searxng-compose.yml down
docker compose -f searxng-compose.yml ps
```

应用内也可以通过 Web API（`/api/searxng/start` / `/api/searxng/stop` /
`/api/searxng/status`，见 `web/routers/searxng_router.py`）来控制同一个
compose 文件，效果等价。

## 3. 配置 config/settings.yaml 指向 SearXNG

编辑（或通过 Web「高级设置」页面修改）`config/settings.yaml` 中的：

```yaml
searxng_port: 8080
searxng_url: "http://127.0.0.1:8080"
```

确保 `searxng_url` 指向实际运行 SearXNG 容器的主机和端口（同机部署时通常
为 `http://127.0.0.1:<SEARXNG_PORT>`）。修改后 Web 后端 `/api/searxng/search`
会基于该 `searxng_url` 拼接搜索请求。
