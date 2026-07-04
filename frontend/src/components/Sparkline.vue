<script setup lang="ts">
import { computed } from 'vue'

// Minimal hand-rolled SVG line chart — deliberately not pulling in a
// charting library (echarts/lightweight-charts) for what's just a grid of
// single-series trend lines. Keeps the bundle small and the rendering logic
// auditable. The SVG is width:100% (viewBox-driven) so it scales down on
// phones/tablets instead of overflowing.
const props = defineProps<{
  points: number[]
  width?: number
  height?: number
  color?: string
  unit?: string
  // 显示峰值（最大值）标签——用于模型级 tok/s 等需要看最大值的图。
  showPeak?: boolean
}>()

const width = computed(() => props.width ?? 260)
const height = computed(() => props.height ?? 70)
const color = computed(() => props.color ?? '#6ea8fe')

// Only finite values participate in min/max/scaling — earlier code did
// Math.min(...points) over an array that could contain NaN (gaps when a
// model wasn't sampled that tick, e.g. an embedding model that was briefly
// unloaded/unreachable), which poisoned min/max to NaN and blanked the whole
// line. NaN/null points are skipped for scaling AND bridged over when drawing
// the path (see below) so the trend line stays continuous instead of
// fragmenting into disjoint segments — the "非连续" a mostly-idle series
// (lots of 0s with occasional missing ticks) otherwise shows.
const valid = computed(() => props.points.filter((v) => v != null && !Number.isNaN(v)))
const minV = computed(() => (valid.value.length ? Math.min(...valid.value) : 0))
const maxV = computed(() => (valid.value.length ? Math.max(...valid.value) : 1))

const path = computed(() => {
  if (valid.value.length < 2) return ''
  const range = maxV.value - minV.value || 1
  const stepX = width.value / Math.max(1, props.points.length - 1)
  let d = ''
  let pen = false // false = next point starts a new sub-path (M)
  props.points.forEach((v, i) => {
    // Skip gaps (null/NaN) but DON'T lift the pen: the next valid point
    // connects back with an `L`, bridging straight across the gap using each
    // point's real index for x, so an isolated missing tick or a short
    // unloaded stretch reads as one continuous line rather than broken
    // fragments. (x still uses the original index i, so the horizontal span
    // of the bridge is geometrically correct.)
    if (v == null || Number.isNaN(v)) {
      return
    }
    const x = i * stepX
    const y = height.value - ((v - minV.value) / range) * height.value
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `
    pen = true
  })
  return d.trim()
})

const latest = computed(() => {
  for (let i = props.points.length - 1; i >= 0; i--) {
    const v = props.points[i]
    if (v != null && !Number.isNaN(v)) return v.toFixed(1)
  }
  return '--'
})

const peak = computed(() => (valid.value.length ? Math.max(...valid.value).toFixed(1) : '--'))
</script>

<template>
  <div class="sparkline-wrap">
    <svg :viewBox="`0 0 ${width} ${height}`" width="100%" :height="height" preserveAspectRatio="none">
      <path v-if="path" :d="path" fill="none" :stroke="color" stroke-width="1.5" />
      <text v-else x="4" y="14" fill="#6b7280" font-size="10">暂无数据</text>
    </svg>
    <div v-if="showPeak" class="sparkline-peak">峰值 {{ peak }}<span v-if="unit" class="unit">{{ unit }}</span></div>
    <div class="sparkline-latest">{{ latest }}<span v-if="unit" class="unit">{{ unit }}</span></div>
  </div>
</template>

<style scoped>
.sparkline-wrap {
  position: relative;
}
.sparkline-wrap svg {
  display: block;
}
.sparkline-latest {
  position: absolute;
  top: 2px;
  right: 4px;
  font-size: 13px;
  font-weight: 600;
  color: #e4e6eb;
}
.sparkline-peak {
  position: absolute;
  top: 2px;
  left: 4px;
  font-size: 10px;
  color: #f59e0b;
}
.unit {
  font-size: 10px;
  color: #9aa3b2;
  margin-left: 2px;
}
</style>
