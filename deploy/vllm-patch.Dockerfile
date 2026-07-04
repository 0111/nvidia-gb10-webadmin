# 派生镜像：在 NVIDIA 官方 vllm 26.06 镜像基础上，修一个会导致 /v1/* 推理接口
# 全部 500 的依赖 bug。
#
# 背景（真机定位）：
#   26.06-py3 内 fastapi==0.137.1 新增了内部路由类 `_IncludedRouter(BaseRoute)`，
#   它没有 `.path` 属性；而 prometheus_fastapi_instrumentator==8.0.0 的中间件
#   会对每个「非排除」请求遍历 app.routes 读 `route.path`，于是遇到
#   `_IncludedRouter` 抛 AttributeError。/metrics、/health 在 excluded_handlers
#   里（200），但 /v1/chat/completions、/v1/embeddings 等不在 → 必 500。
#   现象：模型加载正常、/metrics 正常，但一调用推理接口就 500。
#
# 修法：把 instrumentator 的 routing.py 里 `route_name = route.path` 改成
#   `getattr(route, "path", None)`，让它对没有 .path 的路由对象容错。
#   /metrics 由独立的 Mount 暴露，不受影响，仍正常。
#
# 构建（deploy/bootstrap.sh 会自动幂等执行；也可手动）：
#   docker build -f deploy/vllm-patch.Dockerfile -t gb10-vllm:26.06-py3-patched deploy/
FROM nvcr.io/nvidia/vllm:26.06-py3

RUN sed -i 's/route_name = route\.path/route_name = getattr(route, "path", None)/g' \
    /usr/local/lib/python3.12/dist-packages/prometheus_fastapi_instrumentator/routing.py \
 && python3 -c "import prometheus_fastapi_instrumentator.routing as r, inspect; \
assert 'getattr(route, \"path\", None)' in inspect.getsource(r._get_route_name), 'patch did not apply'; \
print('[vllm-patch] prometheus_fastapi_instrumentator routing.py patched OK')"
