<template>
  <div class="tile-map" ref="containerRef">
    <!-- 瓦片层 -->
    <div class="tile-layer">
      <img
        v-for="tile in tiles"
        :key="tile.key"
        :src="tile.url"
        class="tile"
        :style="{ left: `${tile.left}px`, top: `${tile.top}px` }"
        alt=""
        draggable="false"
      />
    </div>

    <!-- 标注层: 路线 + 景点 -->
    <svg class="marker-layer" :width="size.width" :height="size.height">
      <polyline
        v-for="line in polylines"
        :key="line.key"
        :points="line.points"
        fill="none"
        :stroke="line.color"
        stroke-width="3"
        stroke-linecap="round"
        stroke-linejoin="round"
        opacity="0.85"
      />
      <g v-for="pin in pins" :key="pin.key" class="pin" @click="onPinClick(pin)">
        <circle :cx="pin.x" :cy="pin.y" r="13" :fill="pin.color" stroke="#fff" stroke-width="2" />
        <text :x="pin.x" :y="pin.y + 5" text-anchor="middle" font-size="14" font-weight="bold" fill="#fff">
          {{ pin.index }}
        </text>
        <title>{{ pin.name }}</title>
      </g>
    </svg>

    <!-- 控件 -->
    <div class="tile-map-controls">
      <a-button size="small" @click="changeZoom(1)" :disabled="zoom >= maxZoom">+</a-button>
      <a-button size="small" @click="changeZoom(-1)" :disabled="zoom <= minZoom">-</a-button>
      <a-button size="small" @click="fitAll">全览</a-button>
    </div>

    <div v-if="!pins.length" class="tile-map-empty">暂无带坐标的景点, 无法渲染地图</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

interface MapPoint {
  name: string
  longitude: number
  latitude: number
  dayIndex: number
}

const props = defineProps<{
  points: MapPoint[]
}>()

const emit = defineEmits<{
  (e: 'select', point: MapPoint): void
}>()

const containerRef = ref<HTMLElement | null>(null)
const size = ref({ width: 600, height: 460 })
const zoom = ref(12)

const minZoom = 3
const maxZoom = 17

// 每天一种路线颜色
const DAY_COLORS = ['#1890ff', '#52c41a', '#fa8c16', '#722ed1', '#eb2f96', '#13c2c2', '#f5222d']

const TILE_SIZE = 256
const TILE_HOSTS = ['webrd01', 'webrd02', 'webrd03', 'webrd04']

/** 经纬度 -> 世界像素坐标(标准 Web 墨卡托, 与高德栅格瓦片一致) */
const project = (lng: number, lat: number, z: number) => {
  const n = Math.pow(2, z)
  const sin = Math.sin((lat * Math.PI) / 180)
  const x = ((lng + 180) / 360) * n * TILE_SIZE
  const y = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * n * TILE_SIZE
  return { x, y }
}

const validPoints = computed(() =>
  props.points.filter(
    (p) => Number.isFinite(p.longitude) && Number.isFinite(p.latitude)
  )
)

/** 计算能容纳所有点的最大缩放级别 */
const fitZoom = (points: MapPoint[], width: number, height: number) => {
  if (!points.length) return 12
  const lngs = points.map((p) => p.longitude)
  const lats = points.map((p) => p.latitude)
  const minLng = Math.min(...lngs)
  const maxLng = Math.max(...lngs)
  const minLat = Math.min(...lats)
  const maxLat = Math.max(...lats)
  const availW = Math.max(width - 48, 32)
  const availH = Math.max(height - 48, 32)

  let best = minZoom
  for (let z = minZoom; z <= maxZoom; z++) {
    const a = project(minLng, maxLat, z)
    const b = project(maxLng, minLat, z)
    if (b.x - a.x <= availW && b.y - a.y <= availH) {
      best = z
    } else {
      break
    }
  }
  return best
}

/** 缩放后需要覆盖的瓦片范围与像素原点 */
const viewport = computed(() => {
  const points = validPoints.value
  const { width, height } = size.value
  if (!points.length) {
    return { tiles: [], pins: [], polylines: [], originX: 0, originY: 0 }
  }

  const z = zoom.value
  const projected = points.map((p) => ({ ...p, ...project(p.longitude, p.latitude, z) }))
  const minX = Math.min(...projected.map((p) => p.x))
  const maxX = Math.max(...projected.map((p) => p.x))
  const minY = Math.min(...projected.map((p) => p.y))
  const maxY = Math.max(...projected.map((p) => p.y))

  // 以中心对齐, 保证缩放时不会跑偏
  const centerX = (minX + maxX) / 2
  const centerY = (minY + maxY) / 2
  const originX = centerX - width / 2
  const originY = centerY - height / 2

  const x0 = Math.floor(originX / TILE_SIZE)
  const x1 = Math.floor((originX + width) / TILE_SIZE)
  const y0 = Math.floor(originY / TILE_SIZE)
  const y1 = Math.floor((originY + height) / TILE_SIZE)

  const tiles: { key: string; url: string; left: number; top: number }[] = []
  for (let x = x0; x <= x1; x++) {
    for (let y = y0; y <= y1; y++) {
      const host = TILE_HOSTS[Math.abs(x + y) % TILE_HOSTS.length]
      tiles.push({
        key: `${z}/${x}/${y}`,
        url: `https://${host}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=${x}&y=${y}&z=${z}`,
        left: x * TILE_SIZE - originX,
        top: y * TILE_SIZE - originY
      })
    }
  }

  // 标注序号: 按行程顺序
  const pins = projected.map((p, index) => ({
    key: `${p.name}-${index}`,
    name: p.name,
    index: index + 1,
    x: p.x - originX,
    y: p.y - originY,
    color: DAY_COLORS[p.dayIndex % DAY_COLORS.length],
    point: p
  }))

  // 每天一条连线
  const polylines: { key: string; points: string; color: string }[] = []
  const byDay = new Map<number, typeof pins>()
  pins.forEach((pin) => {
    if (!byDay.has(pin.point.dayIndex)) byDay.set(pin.point.dayIndex, [])
    byDay.get(pin.point.dayIndex)!.push(pin)
  })
  byDay.forEach((items, dayIndex) => {
    if (items.length < 2) return
    polylines.push({
      key: `day-${dayIndex}`,
      points: items.map((i) => `${i.x},${i.y}`).join(' '),
      color: DAY_COLORS[dayIndex % DAY_COLORS.length]
    })
  })

  return { tiles, pins, polylines, originX, originY }
})

const tiles = computed(() => viewport.value.tiles)
const pins = computed(() => viewport.value.pins)
const polylines = computed(() => viewport.value.polylines)

const onPinClick = (pin: { point: MapPoint }) => emit('select', pin.point)

const changeZoom = (delta: number) => {
  const next = zoom.value + delta
  zoom.value = Math.min(maxZoom, Math.max(minZoom, next))
}

const fitAll = () => {
  zoom.value = fitZoom(validPoints.value, size.value.width, size.value.height)
}

const measure = () => {
  const el = containerRef.value
  if (!el) return
  size.value = {
    width: el.clientWidth || 600,
    height: el.clientHeight || 460
  }
}

let observer: ResizeObserver | null = null

onMounted(() => {
  measure()
  fitAll()
  if (containerRef.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => measure())
    observer.observe(containerRef.value)
  }
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

watch(
  () => props.points.length,
  () => fitAll()
)
</script>

<style scoped>
.tile-map {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 460px;
  overflow: hidden;
  background: #eef1f5;
}

.tile-layer {
  position: absolute;
  inset: 0;
}

.tile {
  position: absolute;
  width: 256px;
  height: 256px;
  user-select: none;
}

.marker-layer {
  position: absolute;
  left: 0;
  top: 0;
  pointer-events: none;
}

.pin {
  cursor: pointer;
  pointer-events: auto;
}

.tile-map-controls {
  position: absolute;
  right: 10px;
  top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  z-index: 3;
}

.tile-map-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #999;
  font-size: 13px;
}
</style>
