<!-- Modified for TaleWeft on 2026-10-22: independent identity and retained attribution. -->
<template>
  <section class="about-brand">
    <div class="brand-panel">
      <img class="brand-mark" src="/taleweft-mark.svg" alt="" />
      <div class="brand-main">
        <div class="brand-kicker">{{ BRAND.descriptor }}</div>
        <h2 class="brand-title">{{ BRAND.displayName }}</h2>
        <p class="brand-line">{{ BRAND.tagline }}</p>
      </div>
    </div>

    <div class="info-grid">
      <div class="info-item">
        <span class="info-label">当前版本</span>
        <span class="info-value">{{ release.version }} · {{ release.build_id }}</span>
      </div>
      <div class="info-item">
        <span class="info-label">产品定位</span>
        <span class="info-value">{{ BRAND.chineseName }} · {{ BRAND.tagline }}</span>
      </div>
      <div class="info-item">
        <span class="info-label">发行标识</span>
        <span class="info-value">叙织独立维护版</span>
      </div>
      <div class="info-item">
        <span class="info-label">创作理念</span>
        <span class="info-value">创作由你主导，AI 协助组织与推演</span>
      </div>
    </div>

    <details class="license-panel">
      <summary>许可与致谢</summary>
      <p>本版本基于 PlotPilot v4.6.0 修改，与上游项目及原团队无隶属或背书关系。</p>
      <p>上游版权、贡献者归属及 Apache License 2.0 与 Commons Clause 附加条款予以保留。
        更名不改变原有许可条件；Commons Clause 对收费提供本软件及相关服务存在限制。</p>
      <p>感谢原项目贡献者，以及提供原始部署设计的“梦幻AI动漫”。</p>
      <p class="license-links">
        <a href="https://github.com/shenminglinyi/PlotPilot/tree/v4.6.0" target="_blank" rel="noopener noreferrer">上游源码</a>
        <a href="/legal/LICENSE.txt" target="_blank" rel="noopener noreferrer">项目许可证全文</a>
        <a href="/legal/NOTICE.txt" target="_blank" rel="noopener noreferrer">来源与修改说明</a>
        <a href="/legal/CONTRIBUTORS.txt" target="_blank" rel="noopener noreferrer">原项目贡献者</a>
      </p>
    </details>
  </section>
</template>

<script setup lang="ts">
import { BRAND } from '@/constants/brand'
import { release } from '@/generated/release'
</script>

<style scoped>
.about-brand {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.brand-panel {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 1rem;
  align-items: center;
  padding: 1.125rem;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background:
    linear-gradient(90deg, var(--color-brand-light), transparent 62%),
    var(--app-surface);
}

.brand-mark {
  width: 3.25rem;
  height: 3.25rem;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: var(--color-brand);
  color: #fff;
  font-size: 1.5rem;
  font-weight: 800;
}

.brand-main {
  min-width: 0;
}

.brand-kicker {
  font-size: var(--font-size-xs);
  color: var(--app-text-muted);
  margin-bottom: 0.25rem;
}

.brand-title {
  margin: 0;
  font-size: 1.35rem;
  line-height: 1.25;
  color: var(--app-text-primary);
}

.brand-line {
  margin: 0.375rem 0 0;
  color: var(--app-text-secondary);
  line-height: 1.55;
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.info-item {
  min-width: 0;
  padding: 0.875rem;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-surface);
  color: inherit;
  text-decoration: none;
}

.license-panel {
  padding: 1rem;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  color: var(--app-text-secondary);
  font-size: var(--font-size-sm);
  line-height: 1.6;
}

.license-panel summary {
  cursor: pointer;
  color: var(--app-text-primary);
  font-weight: 600;
}

.license-links {
  display: flex;
  gap: 0.5rem 1rem;
  flex-wrap: wrap;
}

.license-links a {
  color: var(--color-brand);
}

.info-label,
.info-value {
  display: block;
}

.info-label {
  font-size: var(--font-size-xs);
  color: var(--app-text-muted);
  margin-bottom: 0.375rem;
}

.info-value {
  font-size: var(--font-size-sm);
  color: var(--app-text-primary);
  font-weight: 600;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

@media (max-width: 640px) {
  .brand-panel,
  .info-grid {
    grid-template-columns: 1fr;
  }

  .brand-mark {
    width: 2.75rem;
    height: 2.75rem;
  }
}
</style>
