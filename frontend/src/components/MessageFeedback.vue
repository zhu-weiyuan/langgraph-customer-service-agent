<script setup lang="ts">
import type { UiMessage } from '../stores/chat'
import { useChatStore } from '../stores/chat'

const props = defineProps<{ message: UiMessage }>()
const store = useChatStore()

function closeComment() {
  const feedback = props.message.feedback
  if (feedback) feedback.commentOpen = false
}
</script>

<template>
  <div v-if="props.message.feedback">
    <div class="feedback-bar">
      <div class="reaction-group" role="group" aria-label="回复反馈">
        <button
          class="reaction-btn"
          :class="{ on: props.message.feedback.reaction === '👍' }"
          title="有帮助"
          @click="store.react(props.message.id, '👍')"
        >👍</button>
        <button
          class="reaction-btn"
          :class="{ on: props.message.feedback.reaction === '👎' }"
          title="没帮助"
          @click="store.react(props.message.id, '👎')"
        >👎</button>
      </div>

      <div class="star-group" role="group" aria-label="回复评分">
        <button
          v-for="star in 5"
          :key="star"
          class="star-btn"
          :class="{ on: star <= props.message.feedback.stars }"
          :title="`${star} 星`"
          @click="store.rate(props.message.id, star)"
        >★</button>
      </div>

      <span v-if="props.message.feedback.commentSubmitted" class="feedback-thanks">已收到你的意见，感谢反馈</span>
      <span v-else-if="props.message.feedback.stars > 0" class="feedback-thanks">感谢评分</span>
    </div>

    <div v-if="props.message.feedback.commentOpen && !props.message.feedback.commentSubmitted" class="comment-box">
      <textarea
        v-model="props.message.feedback.comment"
        rows="2"
        maxlength="1000"
        placeholder="哪里没有帮到你？你的意见会用于改进回答..."
      ></textarea>
      <div class="comment-actions">
        <button class="ghost-btn" type="button" @click="closeComment">取消</button>
        <button
          class="primary-btn"
          type="button"
          :disabled="!props.message.feedback.comment.trim()"
          @click="store.submitComment(props.message.id)"
        >提交意见</button>
      </div>
    </div>
  </div>
</template>
