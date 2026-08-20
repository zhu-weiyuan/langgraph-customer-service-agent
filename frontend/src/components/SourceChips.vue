<script setup lang="ts">
import { ref } from 'vue'
import type { RagSource } from '../api/client'

const props = defineProps<{ sources: RagSource[] }>()
const open = ref(false)
const expanded = ref<number | null>(null)

function labelOf(source: RagSource, index: number): string {
  return source.title || source.source || source.url || `来源 ${index + 1}`
}

function toggleSnippet(index: number) {
  expanded.value = expanded.value === index ? null : index
}
</script>

<template>
  <div class="sources-block">
    <button class="sources-toggle" type="button" @click="open = !open">
      <span class="caret" :class="{ open }">▶</span>
      引用来源（{{ props.sources.length }}）
    </button>
    <template v-if="open">
      <div class="source-chips">
        <template v-for="(source, index) in props.sources" :key="index">
          <a
            v-if="source.url"
            class="source-chip"
            :href="source.url"
            target="_blank"
            rel="noopener noreferrer"
            :title="source.snippet || labelOf(source, index)"
          >
            <span class="source-index">[{{ index + 1 }}]</span>
            <span class="source-title">{{ labelOf(source, index) }}</span>
            <span v-if="typeof source.score === 'number'" class="source-score">{{ source.score.toFixed(2) }}</span>
          </a>
          <button v-else class="source-chip" type="button" @click="toggleSnippet(index)">
            <span class="source-index">[{{ index + 1 }}]</span>
            <span class="source-title">{{ labelOf(source, index) }}</span>
            <span v-if="typeof source.score === 'number'" class="source-score">{{ source.score.toFixed(2) }}</span>
          </button>
        </template>
      </div>
      <p v-if="expanded !== null && props.sources[expanded]?.snippet" class="source-snippet">
        {{ props.sources[expanded]?.snippet }}
      </p>
    </template>
  </div>
</template>
