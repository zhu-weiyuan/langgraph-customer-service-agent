<script setup lang="ts">
import { computed } from 'vue'
import type { AnalyticsData } from '../api/client'

const props = defineProps<{ analytics: AnalyticsData | null; available: boolean }>()
const emit = defineEmits<{ refresh: [] }>()

interface BarDatum {
  key: string
  label: string
  count: number
  color: string
}

const INTENT_META: Record<string, { label: string; color: string }> = {
  consult: { label: '咨询', color: '#6c8ff8' },
  complaint: { label: '投诉', color: '#f87f8f' },
  chat: { label: '闲聊', color: '#4ecfa5' },
  ending: { label: '结束', color: '#f0b45f' },
}

const EMOTION_META: Record<string, { label: string; color: string }> = {
  neutral: { label: '😐 平静', color: '#8b94b8' },
  angry: { label: '😠 愤怒', color: '#f87f8f' },
  sad: { label: '😢 难过', color: '#a98ef2' },
  anxious: { label: '😰 焦虑', color: '#f59a62' },
  happy: { label: '😊 开心', color: '#4ecfa5' },
}

function toBars(record: Record<string, number> | undefined, meta: Record<string, { label: string; color: string }>): BarDatum[] {
  if (!record) return []
  return Object.entries(record)
    .map(([key, count]) => ({
      key,
      label: meta[key]?.label ?? key,
      count,
      color: meta[key]?.color ?? '#6c8ff8',
    }))
    .sort((a, b) => b.count - a.count)
}

const intentBars = computed(() => toBars(props.analytics?.intents, INTENT_META))
const emotionBars = computed(() => toBars(props.analytics?.emotions, EMOTION_META))

const BAR_H = 22
const CHART_W = 260
const LABEL_W = 74
const COUNT_W = 30

function chartHeight(bars: BarDatum[]): number {
  return Math.max(bars.length * BAR_H, BAR_H)
}

function barWidth(bars: BarDatum[], count: number): number {
  const max = Math.max(...bars.map((b) => b.count), 1)
  const usable = CHART_W - LABEL_W - COUNT_W
  return Math.max((count / max) * usable, 3)
}

const avgRating = computed(() => props.analytics?.ratings?.average ?? 0)
const starLine = computed(() => {
  const rounded = Math.round(avgRating.value)
  return '★★★★★'.slice(0, rounded).padEnd(5, '☆')
})
</script>

<template>
  <aside class="rightbar">
    <div class="insight-header">
      <div><div class="eyebrow">LIVE INSIGHTS</div><h2>服务洞察</h2></div>
      <button class="icon-button" title="刷新数据" @click="emit('refresh')">↻</button>
    </div>

    <template v-if="props.available && props.analytics">
      <div class="metric-grid">
        <div class="metric-card">
          <span>总对话数</span>
          <strong>{{ props.analytics.total_conversations ?? 0 }}</strong>
          <small>所有会话的消息总数</small>
        </div>
        <div class="metric-card">
          <span>平均回复长度</span>
          <strong>{{ Math.round(props.analytics.avg_reply_length ?? 0) }}</strong>
          <small>字符数</small>
        </div>
        <div class="metric-card">
          <span>评分数</span>
          <strong>{{ props.analytics.ratings?.total ?? 0 }}</strong>
          <small>用户主动评分次数</small>
        </div>
        <div class="metric-card">
          <span>工单总数</span>
          <strong>{{ props.analytics.tickets?.total ?? 0 }}</strong>
          <small>由投诉自动创建</small>
        </div>
      </div>

      <section class="insight-section">
        <div class="section-heading"><h3>满意度</h3><span>平均评分</span></div>
        <div class="rating-summary">
          <strong>{{ avgRating ? avgRating.toFixed(1) : '—' }}</strong>
          <span class="stars">{{ starLine }}</span>
          <small>共 {{ props.analytics.ratings?.total ?? 0 }} 条评价</small>
        </div>
      </section>

      <section class="insight-section">
        <div class="section-heading"><h3>意图分布</h3><span>实时</span></div>
        <svg
          v-if="intentBars.length"
          class="svg-chart"
          :viewBox="`0 0 ${CHART_W} ${chartHeight(intentBars)}`"
          role="img"
          aria-label="意图分布柱状图"
        >
          <g v-for="(bar, index) in intentBars" :key="bar.key" :transform="`translate(0, ${index * BAR_H})`">
            <text :x="0" :y="BAR_H / 2 + 3" font-size="10" fill="currentColor" opacity="0.75">{{ bar.label }}</text>
            <rect :x="LABEL_W" :y="BAR_H / 2 - 4" :width="barWidth(intentBars, bar.count)" height="8" rx="4" :fill="bar.color" opacity="0.9" />
            <text :x="LABEL_W + barWidth(intentBars, bar.count) + 6" :y="BAR_H / 2 + 3" font-size="9" fill="currentColor" opacity="0.6">{{ bar.count }}</text>
          </g>
        </svg>
        <p v-else class="empty-insight">开始对话后显示意图分布</p>
      </section>

      <section class="insight-section">
        <div class="section-heading"><h3>情绪分布</h3><span>实时</span></div>
        <svg
          v-if="emotionBars.length"
          class="svg-chart"
          :viewBox="`0 0 ${CHART_W} ${chartHeight(emotionBars)}`"
          role="img"
          aria-label="情绪分布柱状图"
        >
          <g v-for="(bar, index) in emotionBars" :key="bar.key" :transform="`translate(0, ${index * BAR_H})`">
            <text :x="0" :y="BAR_H / 2 + 3" font-size="10" fill="currentColor" opacity="0.75">{{ bar.label }}</text>
            <rect :x="LABEL_W" :y="BAR_H / 2 - 4" :width="barWidth(emotionBars, bar.count)" height="8" rx="4" :fill="bar.color" opacity="0.9" />
            <text :x="LABEL_W + barWidth(emotionBars, bar.count) + 6" :y="BAR_H / 2 + 3" font-size="9" fill="currentColor" opacity="0.6">{{ bar.count }}</text>
          </g>
        </svg>
        <p v-else class="empty-insight">开始对话后显示情绪分布</p>
      </section>

      <section v-if="props.analytics.tickets?.by_priority && Object.keys(props.analytics.tickets.by_priority).length" class="insight-section">
        <div class="section-heading"><h3>工单优先级</h3><span>待处理</span></div>
        <div class="badge-row">
          <span v-for="(count, priority) in props.analytics.tickets.by_priority" :key="priority" class="badge">
            {{ priority }} · {{ count }}
          </span>
        </div>
      </section>
    </template>

    <div v-else class="analytics-empty">
      <div class="orb">◌</div>
      <p>暂无分析数据</p>
      <small>
        /api/analytics 尚未在当前后端提供，
        接入后这里会展示意图、情绪与满意度趋势。
      </small>
    </div>
  </aside>
</template>
