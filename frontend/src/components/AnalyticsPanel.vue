<script setup lang="ts">
import { computed } from 'vue'
import type { AnalyticsData, ObservabilitySnapshot, PrometheusSample } from '../api/client'

const props = defineProps<{
  analytics: AnalyticsData | null
  available: boolean
  observability: ObservabilitySnapshot | null
  observabilityLoading: boolean
}>()
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
  positive: { label: '积极', color: '#4ecfa5' },
  negative: { label: '消极', color: '#f87f8f' },
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
const endpointBars = computed(() => Object.entries(props.observability?.metrics.endpoint_requests ?? {})
  .sort((a, b) => b[1] - a[1])
  .slice(0, 8))
const readyChecks = computed(() => Object.entries(props.observability?.readiness.checks ?? {}))

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

function fmt(value: number | undefined, digits = 0): string {
  if (value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function formatUptime(seconds: number | undefined): string {
  if (!seconds || seconds < 60) return `${fmt(seconds ?? 0)} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时`
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`
}

function statusText(ok: boolean | undefined): string {
  return ok === undefined ? '未知' : ok ? '正常' : '异常'
}

function statusClass(ok: boolean | undefined): string {
  return ok === undefined ? 'unknown' : ok ? 'ok' : 'bad'
}

function sampleLabels(sample: PrometheusSample): string {
  const labels = Object.entries(sample.labels)
  return labels.length ? labels.map(([key, value]) => `${key}=${value}`).join(' · ') : '无标签'
}

function refresh() {
  emit('refresh')
}
</script>

<template>
  <aside class="rightbar">
    <div class="insight-header">
      <div><div class="eyebrow">LIVE INSIGHTS</div><h2>服务洞察</h2></div>
      <button class="icon-button" :class="{ spinning: props.observabilityLoading }" title="刷新数据" @click="refresh">↻</button>
    </div>

    <section v-if="props.observability" class="insight-section ops-section">
      <div class="section-heading"><h3>系统观测</h3><span>{{ props.observabilityLoading ? '刷新中…' : '实时' }}</span></div>
      <div class="ops-status-grid">
        <div class="ops-status-card">
          <span class="ops-status-dot" :class="statusClass(props.observability.health.ok)"></span>
          <div><strong>服务</strong><small>{{ statusText(props.observability.health.ok) }} · v{{ props.observability.health.version ?? '—' }}</small></div>
        </div>
        <div class="ops-status-card">
          <span class="ops-status-dot" :class="statusClass(props.observability.readiness.ready)"></span>
          <div><strong>就绪</strong><small>{{ statusText(props.observability.readiness.ready) }} · {{ readyChecks.length }} 项检查</small></div>
        </div>
        <div class="ops-status-card">
          <span class="ops-status-dot" :class="statusClass(props.observability.health.llm?.reachable)"></span>
          <div><strong>本地 LLM</strong><small>{{ statusText(props.observability.health.llm?.reachable) }}</small></div>
        </div>
        <div class="ops-status-card">
          <span class="ops-status-dot" :class="statusClass(props.observability.health.redis?.available)"></span>
          <div><strong>Redis</strong><small>{{ props.observability.health.redis?.available ? '正常' : '降级' }}</small></div>
        </div>
      </div>
      <div class="ops-check-row">
        <span v-for="([name, check]) in readyChecks" :key="name" class="ops-check" :class="statusClass(check.ok)">
          {{ name === 'postgresql' ? 'PostgreSQL + pgvector' : name }} · {{ statusText(check.ok) }}
        </span>
      </div>
      <div class="ops-meta-line">
        <span>运行 {{ formatUptime(props.observability.health.uptime_seconds) }}</span>
        <span>数据库 {{ fmt(props.observability.health.database?.conversations) }} 条对话</span>
        <span>限流并发 {{ fmt(props.observability.health.rate_limiter?.active_concurrency) }}/{{ fmt(props.observability.health.rate_limiter?.max_concurrency) }}</span>
      </div>
    </section>

    <section v-if="props.observability" class="insight-section">
      <div class="section-heading"><h3>运行指标</h3><span>Prometheus</span></div>
      <div class="ops-metric-grid">
        <div class="metric-card"><span>HTTP 请求</span><strong>{{ fmt(props.observability.metrics.requests_total) }}</strong><small>错误 {{ fmt(props.observability.metrics.errors_total) }}</small></div>
        <div class="metric-card"><span>平均延迟</span><strong>{{ fmt(props.observability.metrics.avg_latency_ms, 0) }}<i>ms</i></strong><small>所有接口平均</small></div>
        <div class="metric-card"><span>LLM 调用</span><strong>{{ fmt(props.observability.metrics.llm_requests_total) }}</strong><small>失败 {{ fmt(props.observability.metrics.llm_errors_total) }}</small></div>
        <div class="metric-card"><span>限流事件</span><strong>{{ fmt(props.observability.metrics.rate_limit_total) }}</strong><small>Redis {{ props.observability.health.redis?.available ? '已接入' : '降级' }}</small></div>
        <div class="metric-card"><span>输入 / 输出 Token</span><strong>{{ fmt(props.observability.metrics.llm_input_tokens) }} / {{ fmt(props.observability.metrics.llm_output_tokens) }}</strong><small>累计消耗</small></div>
        <div class="metric-card"><span>RAG 命中率</span><strong>{{ props.observability.metrics.rag_hit_ratio === null ? '?' : `${(props.observability.metrics.rag_hit_ratio * 100).toFixed(1)}%` }}</strong><small>{{ props.observability.metrics.rag_hit_ratio === null ? '暂无检索数据' : `命中 ${fmt(props.observability.metrics.rag_hits_total)} / ${fmt(props.observability.metrics.rag_queries_total)} 回合` }}</small></div>
        <div class="metric-card"><span>反馈事件</span><strong>{{ fmt(props.observability.metrics.feedback_total) }}</strong><small>点赞、评分、意见</small></div>
        <div class="metric-card"><span>LLM 成本</span><strong>¥{{ props.observability.metrics.llm_cost_yuan.toFixed(2) }}</strong><small>当前进程累计</small></div>
      </div>
    </section>

    <section v-if="props.observability && endpointBars.length" class="insight-section">
      <div class="section-heading"><h3>接口请求</h3><span>Top {{ endpointBars.length }}</span></div>
      <div class="endpoint-list">
        <div v-for="([endpoint, count]) in endpointBars" :key="endpoint" class="endpoint-row">
          <code>{{ endpoint }}</code><span>{{ fmt(count) }}</span><em v-if="props.observability.metrics.endpoint_errors[endpoint]">错误 {{ fmt(props.observability.metrics.endpoint_errors[endpoint]) }}</em>
        </div>
      </div>
    </section>

    <template v-if="props.available && props.analytics">
      <div class="metric-grid">
        <div class="metric-card"><span>总对话数</span><strong>{{ props.analytics.total_conversations ?? 0 }}</strong><small>所有会话的消息总数</small></div>
        <div class="metric-card"><span>平均回复长度</span><strong>{{ Math.round(props.analytics.avg_reply_length ?? 0) }}</strong><small>字符数</small></div>
        <div class="metric-card"><span>评分数</span><strong>{{ props.analytics.ratings?.total ?? 0 }}</strong><small>用户主动评分次数</small></div>
        <div class="metric-card"><span>工单总数</span><strong>{{ props.analytics.tickets?.total ?? 0 }}</strong><small>由投诉自动创建</small></div>
      </div>

      <section class="insight-section">
        <div class="section-heading"><h3>满意度</h3><span>平均评分</span></div>
        <div class="rating-summary"><strong>{{ avgRating ? avgRating.toFixed(1) : '—' }}</strong><span class="stars">{{ starLine }}</span><small>共 {{ props.analytics.ratings?.total ?? 0 }} 条评价</small></div>
      </section>

      <section class="insight-section">
        <div class="section-heading"><h3>意图分布</h3><span>实时</span></div>
        <svg v-if="intentBars.length" class="svg-chart" :viewBox="`0 0 ${CHART_W} ${chartHeight(intentBars)}`" role="img" aria-label="意图分布柱状图">
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
        <svg v-if="emotionBars.length" class="svg-chart" :viewBox="`0 0 ${CHART_W} ${chartHeight(emotionBars)}`" role="img" aria-label="情绪分布柱状图">
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
        <div class="badge-row"><span v-for="(count, priority) in props.analytics.tickets.by_priority" :key="priority" class="badge">{{ priority }} · {{ count }}</span></div>
      </section>
    </template>

    <details v-if="props.observability" class="metric-details">
      <summary>原始 Prometheus 指标（{{ props.observability.metrics.samples.length }} 条）</summary>
      <div class="metric-table">
        <div v-for="(sample, index) in props.observability.metrics.samples" :key="`${sample.name}-${index}`" class="metric-table-row">
          <code>{{ sample.name }}</code><span>{{ sampleLabels(sample) }}</span><strong>{{ fmt(sample.value, 4) }}</strong>
        </div>
      </div>
    </details>

    <div v-if="!props.analytics && !props.observability" class="analytics-empty">
      <div class="orb">◌</div><p>暂无观测数据</p><small>请确认后端已启动，并点击右上角刷新。</small>
    </div>
  </aside>
</template>
