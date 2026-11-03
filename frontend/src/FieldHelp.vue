<script setup lang="ts">
import {
  nextTick,
  onBeforeUnmount,
  ref,
  type CSSProperties,
} from "vue";

const props = withDefaults(
  defineProps<{
    text: string;
    ariaLabel?: string;
  }>(),
  {
    ariaLabel: "字段说明",
  },
);

const trigger = ref<HTMLElement | null>(null);
const popover = ref<HTMLElement | null>(null);
const isVisible = ref(false);
const tooltipStyle = ref<CSSProperties>({});

/** 根据触发图标位置把说明限制在视口内，并优先显示在上方。 */
function positionTooltip(): void {
  if (!trigger.value) {
    return;
  }
  const rect = trigger.value.getBoundingClientRect();
  const width = Math.min(280, window.innerWidth - 32);
  const height = popover.value?.offsetHeight ?? 90;
  const left = Math.min(
    Math.max(16, rect.left + rect.width / 2 - width / 2),
    window.innerWidth - width - 16,
  );
  const above = rect.top - height - 10;
  const top = above >= 12 ? above : rect.bottom + 10;
  tooltipStyle.value = {
    left: `${left}px`,
    top: `${top}px`,
    width: `${width}px`,
  };
}

/** 显示说明，并监听滚动和窗口变化以保持定位。 */
async function showTooltip(): Promise<void> {
  isVisible.value = true;
  await nextTick();
  positionTooltip();
  window.addEventListener("resize", positionTooltip);
  window.addEventListener("scroll", positionTooltip, true);
}

/** 隐藏说明并移除定位监听。 */
function hideTooltip(): void {
  isVisible.value = false;
  window.removeEventListener("resize", positionTooltip);
  window.removeEventListener("scroll", positionTooltip, true);
}

/** 允许键盘用户通过 Escape 关闭字段说明。 */
function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    hideTooltip();
  }
}

onBeforeUnmount(hideTooltip);
</script>

<template>
  <span
    ref="trigger"
    class="field-help"
    tabindex="0"
    role="note"
    :aria-label="props.ariaLabel"
    @mouseenter="showTooltip"
    @mouseleave="hideTooltip"
    @focus="showTooltip"
    @blur="hideTooltip"
    @keydown="handleKeydown"
  >
    i
    <Teleport to="body">
      <span
        v-if="isVisible"
        ref="popover"
        class="field-help-popover"
        role="tooltip"
        :style="tooltipStyle"
      >
        {{ props.text }}
      </span>
    </Teleport>
  </span>
</template>

<style scoped>
.field-help {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: 1px solid rgba(37, 104, 65, 0.42);
  border-radius: 50%;
  color: #286b43;
  background: #eef7ef;
  cursor: help;
  font-size: 0.63rem;
  font-style: normal;
  font-weight: 800;
  line-height: 1;
}

.field-help:focus-visible {
  outline: 3px solid rgba(38, 119, 72, 0.24);
  outline-offset: 2px;
}

.field-help-popover {
  position: fixed;
  z-index: 300;
  border: 1px solid rgba(30, 63, 41, 0.22);
  border-radius: 9px;
  padding: 9px 11px;
  color: #edf7ef;
  background: #203d2a;
  box-shadow: 0 12px 28px rgba(24, 52, 33, 0.22);
  font-size: 0.75rem;
  font-weight: 500;
  line-height: 1.55;
  pointer-events: none;
  text-align: left;
  animation: field-help-in 120ms ease-out;
}

@keyframes field-help-in {
  from {
    opacity: 0;
    transform: translateY(2px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
