<template>
  <div class="xiaoling-action-overlay" role="status" aria-label="小菱正在操作页面">
    <div class="xiaoling-action-frame" aria-hidden="true"></div>
    <div class="xiaoling-action-pill">
      <span class="xiaoling-action-mouse" aria-hidden="true">🖱️</span>
      <span class="xiaoling-action-text">小菱正在操作页面…</span>
    </div>
  </div>
</template>

<style scoped>
.xiaoling-action-overlay {
  position: fixed;
  inset: 0;
  z-index: 2990;
  pointer-events: none;
}
.xiaoling-action-frame {
  position: absolute;
  inset: 0;
  border: 4px solid transparent;
  border-image: conic-gradient(
    from 0deg,
    #ff6b6b,
    #ffd166,
    #4cd97b,
    #4da3ff,
    #9b6bff,
    #ff6b6b
  ) 1;
  animation: xiaoling-frame-hue 4s linear infinite;
}
.xiaoling-action-pill {
  position: absolute;
  top: 24px;
  right: 28px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 9px 16px;
  border: 1px solid rgba(31, 35, 41, 0.08);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 8px 24px rgba(31, 35, 41, 0.14);
  color: #1f2329;
  font-size: 13px;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
}
.xiaoling-action-mouse {
  font-size: 18px;
  line-height: 1;
  animation: xiaoling-mouse-float 1.5s ease-in-out infinite;
}
@keyframes xiaoling-frame-hue {
  to { filter: hue-rotate(360deg); }
}
@keyframes xiaoling-mouse-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-5px); }
}
@media (prefers-reduced-motion: reduce) {
  .xiaoling-action-frame,
  .xiaoling-action-mouse { animation: none; }
}
</style>
