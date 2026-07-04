// TypeScript interfaces mirroring web/schemas.py field-for-field.
// Keep field names in snake_case to match backend JSON exactly.

export type Status = 'ok' | 'warning' | 'error'

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in_seconds: number
}

// ---------------------------------------------------------------------------
// Env doctor
// ---------------------------------------------------------------------------
export interface CheckResultOut {
  name: string
  status: Status
  message: string
  suggested_command: string | null
  fixable: boolean
  details: Record<string, any>
}

export interface EnvReportOut {
  overall_status: Status
  checks: CheckResultOut[]
}

export interface FixRequest {
  confirmed: boolean
  interface?: string | null
}

export interface FixResultOut {
  name: string
  executed: boolean
  command: string
  stdout: string
  stderr: string
  returncode: number | null
  message: string
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------
export type LoadStatus = 'unloaded' | 'loading' | 'running' | 'error'

export interface ModelInfoOut {
  name: string
  path: string
  format: string
  size_bytes: number
  size_gb: number
  is_embedding: boolean
  quantization: string | null
  torch_dtype: string | null
  max_position_embeddings: number | null
  architectures: string[]
  model_type: string | null
  tool_call_capable: boolean
  multimodal: boolean
  modalities: string[]
  is_moe: boolean
  num_experts: number | null
  num_experts_per_tok: number | null
  param_count: number | null
  hidden_size: number | null
  num_hidden_layers: number | null
  engine_hint: string
  gguf_multi_shard: boolean
  gguf_vllm_compatible: boolean | null
  warnings: string[]
  valid: boolean
  validation_errors: string[]
  load_status: LoadStatus
}

export interface ModelListResponse {
  model_root_dir: string
  total: number
  models: ModelInfoOut[]
  scanned_at: number | null
}

export interface ParamEntryOut {
  default: any
  editable: boolean
  options: any[] | null
  source: string
}

export interface GpuMemoryHint {
  mem_free_gb: number | null
  mem_total_gb: number | null
  suggested_max: number | null
}

export interface ParamsResponse {
  model_name: string
  engine: 'vllm'
  params: Record<string, ParamEntryOut>
  gpu_memory_hint?: GpuMemoryHint | null
}

export interface ModelLoadRequest {
  engine: 'vllm'
  params: Record<string, any>
  host_port?: number | null
}

export interface CommandResult {
  ok: boolean
  command: string
  stdout: string
  stderr: string
  returncode: number | null
  error: string | null
}

export interface ModelLoadResponse {
  model_name: string
  accepted: boolean
  message: string
  compose_path: string | null
  command_result: CommandResult | null
}

export interface ModelUnloadResponse {
  model_name: string
  accepted: boolean
  message: string
  command_result: CommandResult | null
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------
export interface ComponentOut {
  name: string
  container_name: string | null
  port: number | null
  bind_host: string | null
  memory_usage_mb: number | null
  status: string
  detail: string
  manageable: boolean
}

export interface ComponentListResponse {
  components: ComponentOut[]
}

export interface ComponentActionResponse {
  name: string
  action: 'start' | 'stop' | 'restart'
  ok: boolean
  message: string
  command_result: CommandResult | null
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------
export interface LogsResponse {
  component: string
  lines: number
  content: string
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
export interface ModelLoadSummaryOut {
  general_models_loaded: string[]
  embedding_models_loaded: string[]
  general_models_failed: string[]
  embedding_models_failed: string[]
}

export interface SystemResourcesOut {
  cpu_percent: number | null
  gpu_percent: number | null
  power_watts: number | null
  mem_used_gb: number | null
  mem_free_gb: number | null
  cache_gb: number | null
}

export interface OverviewResponse {
  model_load: ModelLoadSummaryOut
  env: EnvReportOut
  resources: SystemResourcesOut
  components: ComponentOut[]
}

// ---------------------------------------------------------------------------
// Debug chat
// ---------------------------------------------------------------------------
export interface DebugChatRequest {
  model_name: string
  base_url?: string | null
  api_format: 'openai' | 'claude'
  system?: string | null
  prompt: string
  max_tokens: number
  temperature: number
  extra: Record<string, any>
}

export interface DebugChatResponse {
  request_payload: Record<string, any>
  status_code: number | null
  response_payload: Record<string, any> | null
  error: string | null
}

// ---------------------------------------------------------------------------
// Performance test
// ---------------------------------------------------------------------------
export interface PerfRunRequest {
  model_name: string
  concurrency: number
  num_requests: number
  prompt: string
  max_tokens: number
  temperature: number
  stream: boolean
}

export interface PerfRequestResult {
  index: number
  ok: boolean
  status_code: number | null
  ttft_ms: number | null
  total_ms: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  error: string | null
  request_excerpt: Record<string, any> | null
  response_excerpt: string | null
}

export interface PerfProgress {
  report_id: string
  model_name: string
  completed: number
  total: number
  failed: number
  stage: string
}

export interface PerfRunSummary {
  total_requests: number
  successful: number
  failed: number
  total_duration_ms: number
  avg_total_ms: number | null
  avg_ttft_ms: number | null
  total_completion_tokens: number
  throughput_tokens_per_sec: number | null
}

export interface PerfReportOut {
  report_id: string
  created_at: number
  model_name: string
  concurrency: number
  num_requests: number
  max_tokens: number
  summary: PerfRunSummary
  results: PerfRequestResult[]
}

export interface PerfReportListItem {
  report_id: string
  created_at: number
  model_name: string
  concurrency: number
  num_requests: number
  summary: PerfRunSummary
}

export interface PerfReportListResponse {
  reports: PerfReportListItem[]
}

// ---------------------------------------------------------------------------
// Metrics history
// ---------------------------------------------------------------------------
export interface MetricsHistoryPoint {
  ts: number
  cpu_percent: number | null
  mem_used_gb: number | null
  mem_free_gb: number | null
  cache_gb: number | null
  gpu_percent: number | null
  gpu_temp_c: number | null
  power_watts: number | null
  // 每模型 tok/s 快照：{模型名: {prefill_tps, decode_tps, ...}}
  model_tps?: Record<string, { prefill_tps: number | null; decode_tps: number | null }> | null
}

export interface MetricsHistoryResponse {
  window: string
  points: MetricsHistoryPoint[]
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
export interface SettingsOut {
  web_port: number
  web_host: string
  admin_username: string
  model_root_dir: string
  data_dir: string
  searxng_port: number
  searxng_url: string
  vllm_image: string
  cuda_compat_dir: string
  searxng_proxy_url: string | null
  frontend_port: number
  // Shown in plaintext per project requirement (single-admin internal tool).
  secret_key: string
  admin_password: string
  vllm_api_key: string
}

export interface SettingsUpdateRequest {
  web_port?: number | null
  web_host?: string | null
  admin_username?: string | null
  admin_password?: string | null
  model_root_dir?: string | null
  data_dir?: string | null
  searxng_port?: number | null
  searxng_url?: string | null
  vllm_image?: string | null
  cuda_compat_dir?: string | null
  searxng_proxy_url?: string | null
  frontend_port?: number | null
  vllm_api_key?: string | null
  rotate_secret_key?: boolean
}

// ---------------------------------------------------------------------------
// SearXNG
// ---------------------------------------------------------------------------
export interface SearxngStatusResponse {
  running: boolean
  command_result: CommandResult | null
}

export interface SearxngActionResponse {
  ok: boolean
  message: string
  command_result: CommandResult | null
}

export interface SearxngSearchResponse {
  ok: boolean
  query: string
  data: Record<string, any> | null
  error: string | null
}

export interface SearxngProxyTestRequest {
  proxy?: string | null
}

export interface SearxngProxyTestResponse {
  ok: boolean
  latency_ms: number | null
  status_code: number | null
  error: string | null
}

// ---------------------------------------------------------------------------
// WebSocket envelope
// ---------------------------------------------------------------------------
export interface WsEnvelope<T = any> {
  topic: string
  data: T
  ts: number
}

export interface MetricsData {
  cpu_percent: number | null
  mem_used_gb: number | null
  mem_free_gb: number | null
  cache_gb: number | null
  gpu_percent: number | null
  gpu_temp_c: number | null
  power_watts: number | null
}

export interface LoadProgressData {
  model_name: string
  stage: 'starting' | 'running' | 'error' | 'unloaded'
  message: string
}

// ---------------------------------------------------------------------------
// API directory (实时总览 -> API 发布)
// ---------------------------------------------------------------------------
export interface ApiDirectoryEntry {
  name: string
  purpose: string
  base_url: string | null
  auth_hint: string
}

export interface ApiDirectoryResponse {
  entries: ApiDirectoryEntry[]
}

export interface ApiHealthResult {
  name: string
  target_url: string | null
  healthy: boolean
  status_code: number | null
  latency_ms: number | null
  detail: string
}

export interface ApiHealthCheckResponse {
  overall_healthy: boolean
  checked_at: number
  results: ApiHealthResult[]
}

// ---------------------------------------------------------------------------
// Runtime stats (运行观测 -> GET /api/models/{name}/runtime-stats)
// ---------------------------------------------------------------------------
export interface RuntimeVllmMetrics {
  num_requests_running: number | null
  num_requests_waiting: number | null
  gpu_cache_usage_perc: number | null
}

export interface RuntimeStatsResponse {
  model_name: string
  loaded: boolean
  message?: string
  engine?: string
  container_name?: string
  host_port?: number | null
  launch_command?: string[] | null
  memory_usage_mb?: number | null
  metrics?: RuntimeVllmMetrics | null
  metrics_message?: string | null
}
