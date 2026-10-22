<!-- Modified for TaleWeft on 2026-10-22: replace upstream promotional footer. -->
<template>
  <footer class="brand-footer" aria-label="产品信息">
    <span class="brand-footer__name">{{ BRAND.displayName }}</span>
    <span class="brand-footer__text">{{ BRAND.tagline }}</span>
    <span class="brand-footer__descriptor">{{ BRAND.descriptor }}</span>
  </footer>
</template>

<script setup lang="ts">
import { BRAND } from '@/constants/brand'
</script>

<style scoped>
.brand-footer {
  position: relative;
  z-index: 1;
  text-align: center;
  padding: 28px 20px 32px;
  margin-top: 40px;
  border-top: 1px solid var(--app-border);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--app-text-muted);
  line-height: 1.6;
}

.brand-footer__name {
  font-weight: 700;
  color: var(--color-gold);
  letter-spacing: 0.03em;
}

.brand-footer__descriptor {
  flex-basis: 100%;
}

.brand-footer__text {
  color: var(--app-text-muted);
}

</style>
