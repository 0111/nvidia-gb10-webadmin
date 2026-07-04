<script setup lang="ts">
// 嵌入式模型配置页：复用 GeneralModelView 的全部逻辑（模型选择/参数表单/
// 加载/日志），只是过滤模型类型为 embedding，且对应组件容器名不同
// （gb10-vllm-embedding）。embedding 模型的 vllm 参数推荐里 convert 已被
// core.param_advisor 强制设为 "embed" 且 editable=false，max_model_len
// 默认 4k（通用模型默认 64k），均由后端 param_advisor 决定，无需在前端
// 重复处理。
import GeneralModelView from './GeneralModelView.vue'
</script>

<template>
  <GeneralModelView :embedding="true" />
</template>
