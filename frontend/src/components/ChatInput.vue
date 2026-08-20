<script setup lang="ts">
import { computed, ref } from 'vue'

const MAX_LENGTH = 4000

const props = defineProps<{ loading: boolean; canCancel: boolean }>()
const emit = defineEmits<{ send: [message: string]; cancel: [] }>()

const text = ref('')
const remaining = computed(() => MAX_LENGTH - text.value.length)

const QUICK_PROMPTS = [
  { label: '设备连接', text: '音箱怎么连接 Wi‑Fi？' },
  { label: '查询订单', text: '我的订单什么时候能到？' },
  { label: '售后政策', text: '保修期是多久？' },
  { label: '我要投诉', text: '我要投诉产品质量问题' },
]

function submit() {
  const value = text.value.trim()
  if (!value || props.loading || value.length > MAX_LENGTH) return
  emit('send', value)
  text.value = ''
}

function setText(value: string) {
  text.value = value
}

defineExpose({ setText })
</script>

<template>
  <div class="composer-zone">
    <div class="quick-prompts">
      <button v-for="prompt in QUICK_PROMPTS" :key="prompt.label" type="button" @click="setText(prompt.text)">
        {{ prompt.label }}
      </button>
    </div>
    <div class="input-bar">
      <textarea
        v-model="text"
        :maxlength="MAX_LENGTH"
        placeholder="输入客户问题，Enter 发送，Shift + Enter 换行..."
        @keydown.enter.exact.prevent="submit"
      />
      <button v-if="props.canCancel" class="cancel-button" type="button" @click="emit('cancel')">
        停止
      </button>
      <button v-else class="send-button" :disabled="props.loading || !text.trim()" type="button" @click="submit">
        <span>{{ props.loading ? '处理中' : '发送' }}</span><b>↑</b>
      </button>
    </div>
    <div class="composer-footnote">
      <span>Enter</span> 发送 <i></i> AI 回复基于知识库生成，请注意甄别
      <em class="char-count" :class="{ limit: remaining < 200 }">{{ text.length }}/{{ MAX_LENGTH }}</em>
    </div>
  </div>
</template>
