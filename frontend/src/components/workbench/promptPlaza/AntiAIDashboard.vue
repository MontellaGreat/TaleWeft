<template>
  <div class="anti-ai-dashboard">
    <!-- 顶部概览 -->
    <div class="dashboard-header">
      <div class="header-info">
        <h3 class="dashboard-title">Anti-AI 防御系统</h3>
        <p class="dashboard-subtitle">写作规则与章后扫描 · 可用能力不代表本章已执行</p>
      </div>
      <div class="header-actions">
        <n-button size="small" type="primary" secondary @click="showTutorial = true">
          使用教程
        </n-button>
      </div>
    </div>

    <!-- 子标签页 -->
    <div class="sub-tabs">
      <div
        v-for="tab in subTabs"
        :key="tab.key"
        class="sub-tab"
        :class="{ 'is-active': activeSubTab === tab.key }"
        @click="activeSubTab = tab.key"
      >
        {{ tab.label }}
      </div>
    </div>

    <!-- ═══════════════════════════════════
         概览面板
         ═══════════════════════════════════ -->
    <template v-if="activeSubTab === 'overview'">
      <!-- 七层防御状态 -->
      <div class="layers-grid">
        <div
          v-for="layer in defenseLayers"
          :key="layer.key"
          class="layer-card"
          :class="{ 'is-active': layer.active }"
          :style="{ '--layer-color': layer.color }"
        >
          <div v-if="layer.icon" class="layer-icon">{{ layer.icon }}</div>
          <div class="layer-info">
            <div class="layer-name">{{ layer.name }}</div>
            <div class="layer-desc">{{ layer.desc }}</div>
          </div>
          <div class="layer-status">
            <n-tag :type="layer.failed ? 'error' : layer.active ? 'success' : 'default'" size="tiny" :bordered="false">
              {{ layer.status }}
            </n-tag>
          </div>
        </div>
      </div>
      <p v-if="statsError" role="alert">{{ statsError }}</p>
      <p v-if="antiAIStats" class="section-desc">{{ historyNovelId ? '所选小说' : '全部小说' }}累计审计 {{ antiAIStats.observed_audit_runs || 0 }} 次，其中历史失败 {{ antiAIStats.failed_audit_runs || 0 }} 次。层状态以最近一次结果为准；能力不代表当前章节正在执行。</p>
      <p v-if="antiAIStats" class="section-desc">正文流式扫描 {{ antiAIStats.observed_stream_runs || 0 }} 次；流式扫描与章后审计分别记录，流式完成不代表正文已提交。</p>

      <!-- 系统统计 -->
      <div v-if="antiAIStats" class="stats-section">
        <h4 class="section-title">系统统计</h4>
        <div class="stats-grid">
          <div class="stat-card">
            <div class="stat-number">{{ antiAIStats.total_prompts }}</div>
            <div class="stat-desc">总提示词数</div>
          </div>
          <div class="stat-card">
            <div class="stat-number accent">{{ antiAIStats.anti_ai_prompts }}</div>
            <div class="stat-desc">Anti-AI 提示词</div>
          </div>
          <div class="stat-card">
            <div class="stat-number">{{ antiAIStats.cliche_patterns }}</div>
            <div class="stat-desc">俗套检测模式</div>
          </div>
          <div class="stat-card">
            <div class="stat-number">{{ antiAIStats.categories_count }}</div>
            <div class="stat-desc">分类数</div>
          </div>
        </div>
      </div>
    </template>

    <template v-if="activeSubTab === 'history'">
      <n-select v-model:value="historyNovelId" :options="novelOptions" placeholder="选择小说查看自动审计记录" />
      <n-button size="small" :loading="historyLoading" :disabled="!historyNovelId" @click="loadHistory">刷新记录</n-button>
      <p class="section-desc">扫描检测规则命中；改写根据作品策略、规则评分和声线漂移触发。旧记录没有正文版本，不能证明当前正文已经扫描。</p>
      <p v-if="historyError" role="alert">{{ historyError }}</p>
      <p v-if="progressError" role="alert">{{ progressError }}</p>
      <div v-for="run in scanProgress" :key="run.run_id" class="stat-card" role="status">
        <strong>独立扫描进度 · 第{{ run.chapter_number }}章 · {{ progressStageLabel(run.stage) }}</strong>
        <p>运行 {{ run.run_id }} · 任务 {{ run.job_id || '未关联' }} · 正文修订 {{ run.chapter_revision ?? '未匹配已提交版本' }}</p>
        <p>扫描 {{ scanStatusLabel(run.scan_status) }} · 审计归档 {{ run.persistence_status === 'committed' ? '已提交' : run.persistence_status === 'failed' ? '失败' : '等待提交' }}</p>
        <p :title="run.content_hash">正文版本 {{ run.content_hash.slice(0, 16) }} · 规则版本 {{ run.rule_version.slice(0, 16) || '扫描开始前尚未确认' }}</p>
        <p v-if="run.error">{{ run.error }}</p>
      </div>
      <div v-if="historyNovelId && trend.length" class="stat-card">
        <strong>最近章节趋势</strong>
        <span v-for="point in trend" :key="point.chapter_number" class="trend-point">
          第{{ point.chapter_number }}章 {{ point.severity_score.toFixed(1) }}分 / {{ point.total_hits }}命中
          <em v-if="point.legacy">（旧摘要，无版本证据）</em>
          <em v-else-if="point.latest_scan_status !== 'completed' || point.latest_persistence_status !== 'committed'">（最近扫描或归档未完成；显示上次摘要）</em>
        </span>
      </div>
      <n-spin :show="historyLoading">
        <n-empty v-if="!historyLoading && !historyError && !progressError && !scanProgress.length && !auditRuns.length && !legacyAudits.length && !streamRuns.length && !promptRuns.length" description="尚未运行自动审计；暂无可核验记录" />
        <details v-for="run in promptRuns" :key="run.run_id" class="stat-card">
          <summary>写前规则执行 · 第 {{ run.chapter_number || '未知' }} 章 · {{ scanStatusLabel(run.status) }}</summary>
          <p>运行 {{ run.run_id }} · 任务 {{ run.job_id || '未关联' }}</p>
          <p>场景 {{ SCENE_TYPE_LABELS[run.evidence.scene_type] || run.evidence.scene_type }} · 输入估算 {{ run.evidence.input_token_estimate }}/{{ run.evidence.input_budget_tokens }} tokens</p>
          <p v-for="(state, layer) in run.evidence.layers" :key="layer">{{ layer }}：{{ state.applied ? '已注入本次模型提示词' : '本次不可用' }} {{ state.reason || '' }}</p>
          <p :title="run.prompt_hash">提示词版本 {{ run.prompt_hash.slice(0, 16) }} · 规则版本 {{ run.rule_version.slice(0, 16) }}</p>
          <p v-if="run.error">{{ run.error }}</p>
        </details>
        <details v-for="run in streamRuns" :key="run.run_id" class="stat-card">
          <summary>流式扫描 · 第 {{ run.chapter_number || '未知' }} 章 · {{ streamStatusLabel(run.status) }}</summary>
          <p>运行 {{ run.run_id }} · 任务 {{ run.job_id || '未关联' }}</p>
          <p>分块扫描 {{ run.summary.chunks_scanned }} 次 · 增量命中 {{ run.summary.incremental_hits }} · 最终复扫 {{ scanStatusLabel(run.scan_status) }}</p>
          <p>正文版本 {{ run.content_hash.slice(0, 16) }} · 规则版本 {{ run.rule_version.slice(0, 16) }} · {{ run.action === 'warn' ? '告警并保留片段' : '显式阈值停止' }}</p>
          <p v-if="run.error">{{ run.error }}</p>
        </details>
        <details v-for="run in auditRuns" :key="run.run_id" class="stat-card">
          <summary>第 {{ run.chapter_number }} 章 · {{ run.created_at }} · 扫描 {{ scanStatusLabel(run.scan_status) }} · 记录 {{ run.persistence_status === 'committed' ? '已提交' : '未提交' }}</summary>
          <p v-if="run.scan_status === 'completed'">{{ run.report.metrics.overall_assessment }} · 命中 {{ run.report.metrics.total_hits }} · 规则评分 {{ run.report.metrics.severity_score }}</p>
          <p v-if="run.error">{{ run.error }}</p>
          <p :title="run.content_hash">正文版本 {{ run.content_hash.slice(0, 16) }}</p>
          <p :title="run.rule_version">规则版本 {{ run.rule_version.slice(0, 16) }}</p>
          <p v-for="(rec, i) in run.report.recommendations" :key="i">{{ rec }}</p>
          <p v-for="(hit, i) in run.report.hits" :key="i">{{ hit.pattern }}：{{ hit.text }}（{{ hit.start }}–{{ hit.end }}）</p>
        </details>
        <template v-if="legacyAudits.length">
          <h4>章节审计摘要（兼容历史记录）</h4>
          <details v-for="(audit, i) in legacyAudits" :key="i" class="stat-card">
            <summary>第 {{ audit.chapter_number }} 章 · {{ audit.overall_assessment }} · {{ audit.created_at }}</summary>
            <p>规则命中 {{ audit.total_hits }} · 评分 {{ audit.severity_score }}</p>
            <p v-for="(rec, j) in (audit.recommendations as string[])" :key="j">{{ rec }}</p>
          </details>
        </template>
      </n-spin>
    </template>

    <template v-if="activeSubTab === 'review'">
      <n-select v-model:value="historyNovelId" :options="novelOptions" placeholder="选择小说查看改写候选与策略" />
      <AntiAIRewriteReview :novel-id="historyNovelId" />
    </template>

    <!-- ═══════════════════════════════════
         快速扫描面板
         ═══════════════════════════════════ -->
    <template v-if="activeSubTab === 'scan'">
      <div class="scan-section">
        <p class="section-desc">粘贴一段文本，检测其中的 AI 味模式</p>
        <n-input
          v-model:value="scanInput"
          type="textarea"
          :autosize="{ minRows: 5, maxRows: 14 }"
          placeholder="在此粘贴要检测的文本…"
          class="scan-input"
        />
        <div class="scan-actions">
          <n-button
            type="primary"
            :loading="scanning"
            :disabled="!scanInput.trim()"
            @click="handleScan"
          >
            开始扫描
          </n-button>
          <n-button v-if="scanInput.trim()" quaternary @click="scanInput = ''">
            清空
          </n-button>
        </div>

        <!-- 扫描结果 -->
        <div v-if="scanResult" class="scan-result">
          <div class="result-header">
            <div class="result-assessment" :style="{ color: assessmentColor }">
              {{ scanResult.overall_assessment }}
            </div>
            <p class="section-desc scan-provenance">
              本次手工扫描为临时结果，未写入自动审计历史。运行标识：{{ scanResult.run_id || '未生成' }}
            </p>
            <div class="result-score">
              严重性分数：<strong>{{ scanResult.severity_score }}</strong>/100
            </div>
          </div>

          <div class="result-stats">
            <div class="stat-item">
              <span class="stat-value critical">{{ scanResult.critical_hits }}</span>
              <span class="stat-label">严重</span>
            </div>
            <div class="stat-item">
              <span class="stat-value warning">{{ scanResult.warning_hits - scanResult.critical_hits > 0 ? scanResult.warning_hits - scanResult.critical_hits : scanResult.total_hits - scanResult.critical_hits }}</span>
              <span class="stat-label">警告</span>
            </div>
            <div class="stat-item">
              <span class="stat-value">{{ scanResult.total_hits }}</span>
              <span class="stat-label">总命中</span>
            </div>
          </div>

          <!-- 分类分布 -->
          <div v-if="Object.keys(scanResult.category_distribution).length" class="category-dist">
            <h5 class="sub-title">分类分布</h5>
            <div class="dist-bars">
              <div
                v-for="(count, cat) in scanResult.category_distribution"
                :key="cat"
                class="dist-row"
              >
                <span class="dist-cat">{{ cat }}</span>
                <div class="dist-bar-bg">
                  <div
                    class="dist-bar-fill"
                    :style="{ width: `${(count / scanResult.total_hits) * 100}%` }"
                  ></div>
                </div>
                <span class="dist-count">{{ count }}</span>
              </div>
            </div>
          </div>

          <!-- 改进建议 -->
          <div v-if="scanResult.improvement_suggestions && scanResult.improvement_suggestions.length" class="suggestions">
            <h5 class="sub-title">改进建议</h5>
            <div
              v-for="(sug, idx) in scanResult.improvement_suggestions"
              :key="idx"
              class="suggestion-item"
            >
              {{ sug }}
            </div>
          </div>

          <!-- 修改建议 -->
          <div v-if="scanResult.recommendations && scanResult.recommendations.length" class="recommendations">
            <h5 class="sub-title">修改建议</h5>
            <div
              v-for="(rec, idx) in scanResult.recommendations"
              :key="idx"
              class="recommendation-item"
            >
              {{ rec }}
            </div>
          </div>

          <!-- 命中详情 -->
          <div v-if="scanResult.hits && scanResult.hits.length" class="hits-list">
            <h5 class="sub-title">命中详情 ({{ scanResult.hits.length }})</h5>
            <div
              v-for="(hit, idx) in scanResult.hits.slice(0, 30)"
              :key="idx"
              class="hit-item"
              :class="`severity-${hit.severity}`"
            >
              <n-tag :type="severityTagType(hit.severity)" size="tiny" :bordered="false">
                {{ hit.severity }}
              </n-tag>
              <span class="hit-pattern">{{ hit.pattern }}</span>
              <code class="hit-text">{{ hit.text }}</code>
              <span v-if="hit.replacement_hint" class="hit-hint">→ {{ hit.replacement_hint }}</span>
            </div>
            <div v-if="scanResult.hits.length > 30" class="more-hits">
              还有 {{ scanResult.hits.length - 30 }} 处命中…
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══════════════════════════════════
         规则面板
         ═══════════════════════════════════ -->
    <template v-if="activeSubTab === 'rules'">
      <div class="rules-section">
        <p class="section-desc">
          正向行为映射规则：将"禁止 X"重构为"当遇到场景 Y 时，必须执行 Z"，
          避免否定指令在 Transformer Self-Attention 中激活被禁止的 Token。
        </p>

        <div v-if="rulesLoading" class="loading-wrap">
          <n-spin size="small" />
        </div>

        <div v-else-if="rules.length" class="rules-list">
          <div
            v-for="rule in rules"
            :key="rule.key"
            class="rule-card"
          >
            <div class="rule-header">
              <n-tag :type="severityTagType(rule.severity)" size="tiny" :bordered="false">
                {{ rule.severity }}
              </n-tag>
              <span class="rule-anti">{{ rule.anti_pattern }}</span>
              <n-tag size="tiny" :bordered="false" type="info">{{ rule.category }}</n-tag>
            </div>
            <div class="rule-action">
              <span class="rule-label">正向动作：</span>
              {{ rule.positive_action }}
            </div>
          </div>
        </div>

        <n-empty v-else description="暂无规则数据" />
      </div>
    </template>

    <!-- ═══════════════════════════════════
         白名单面板
         ═══════════════════════════════════ -->
    <template v-if="activeSubTab === 'allowlist'">
      <div class="allowlist-section">
        <p class="section-desc">
          在战斗/悬疑/恐怖/告白等特定场景中，部分"AI味"模式是被允许的。
          白名单不等于滥用——即使在允许的场景中也有密度限制。
        </p>

        <div v-if="allowlistLoading" class="loading-wrap">
          <n-spin size="small" />
        </div>

        <div v-else-if="allowlistScenes.length" class="scenes-list">
          <div
            v-for="scene in allowlistScenes"
            :key="scene.scene_type"
            class="scene-card"
          >
            <div class="scene-header">
              <span class="scene-type-label">{{ getSceneLabel(scene.scene_type) }}</span>
              <code class="scene-key">{{ scene.scene_type }}</code>
              <n-tag size="tiny" :bordered="false" type="info">
                密度上限: {{ scene.max_density_per_1000 }}/千字
              </n-tag>
            </div>
            <p class="scene-desc">{{ scene.description }}</p>
            <div v-if="scene.allowed_categories.length" class="scene-categories">
              <span class="scene-label">豁免分类：</span>
              <n-tag
                v-for="cat in scene.allowed_categories"
                :key="cat"
                size="tiny"
                type="success"
                :bordered="false"
              >
                {{ cat }}
              </n-tag>
            </div>
            <div v-if="scene.allowed_patterns.length" class="scene-patterns">
              <span class="scene-label">豁免模式：</span>
              <n-tag
                v-for="pat in scene.allowed_patterns"
                :key="pat"
                size="tiny"
                :bordered="false"
              >
                {{ pat }}
              </n-tag>
            </div>
          </div>
        </div>

        <n-empty v-else description="暂无白名单数据" />
      </div>
    </template>

    <!-- 教程弹窗 -->
    <n-modal
      v-model:show="showTutorial"
      preset="card"
      title="Anti-AI 防御系统使用教程"
      style="max-width: 720px"
      :bordered="true"
    >
      <div class="tutorial-content">
        <section class="tutorial-section">
          <h4>这是什么？</h4>
          <p>
            Anti-AI 防御系统提供写作规则、正文流式扫描和章后复验，帮助识别和修订常见模式。
            规则命中属于修改线索，需结合场景和作者判断；流程执行完成不代表文学质量保证。
          </p>
          <p>
            正向行为映射把"禁止X"补充为"当遇到Y时执行Z"，给模型提供具体的替代动作。
            模型是否遵循这些提示词仍需通过生成结果和章后扫描检查。
          </p>
        </section>

        <section class="tutorial-section">
          <h4>七层防御体系</h4>
          <div class="tutorial-layers">
            <div class="tl-item">
              <strong>L1 正向行为映射</strong>：把"禁止X"补充为"当遇到Y时执行Z"，提供可执行的替代动作。
            </div>
            <div class="tl-item">
              <strong>L2 核心协议 P1-P5</strong>：信息密度法则、感官优先法则、角色差异化法则、节奏法则、衔接法则。
            </div>
            <div class="tl-item">
              <strong>L3 场景化白名单</strong>：战斗场景允许生理描写，悬疑场景允许微表情，不同场景有不同豁免。
            </div>
            <div class="tl-item">
              <strong>L4 角色状态向量</strong>：按本小说和本次出场角色读取声线、习惯及已记录状态；缺少锚点时明确显示不可用。
            </div>
            <div class="tl-item">
              <strong>L5 上下文配额</strong>：保留核心协议和原始事实，按完整角色锚点分配余量；输入超出预算时明确阻断。
            </div>
            <div class="tl-item">
              <strong>L6 流式扫描</strong>：分块检测正文，默认告警并保留片段；可在策略中显式选择按严重命中阈值停止。
            </div>
            <div class="tl-item">
              <strong>L7 章后审计</strong>：35+ 模式检测、指标趋势追踪、自适应学习新模式。
            </div>
          </div>
        </section>

        <section class="tutorial-section">
          <h4>如何使用？</h4>
          <ol class="tutorial-steps">
            <li>在 <strong>改写候选与策略</strong> 中选择写作场景规则、改写模式和流式告警或停止策略</li>
            <li>使用<strong>快速扫描</strong>标签页检测文本中的 AI 味</li>
            <li>在<strong>规则</strong>标签页中查看正向行为映射规则</li>
            <li>在<strong>白名单</strong>标签页中了解各场景的豁免规则</li>
            <li>正文生成前冻结行为协议、场景规则及可用角色锚点，写前规则执行历史展示实际模型调用的提示词版本</li>
            <li>章节生成后，系统会自动运行 Anti-AI 审计管线</li>
            <li>在 API 端点 <code>/api/v1/anti-ai/scan</code> 中可以程序化调用扫描</li>
          </ol>
        </section>

        <section class="tutorial-section">
          <h4>35+ 检测模式一览</h4>
          <p>系统内置 35+ 种 AI 味检测模式，覆盖以下分类：</p>
          <div class="pattern-categories">
            <div class="pc-item"><strong>微表情</strong>：嘴角上扬、眼里闪过、指尖泛白、一丝系列、下意识等</div>
            <div class="pc-item"><strong>声线</strong>：带语气前缀、声线变化、字字带X、不容置疑等</div>
            <div class="pc-item"><strong>比喻</strong>：仿佛/宛如/犹如、心湖涟漪、小动物比喻等</div>
            <div class="pc-item"><strong>生理性</strong>：生理性泪水/水雾、生理性前缀等</div>
            <div class="pc-item"><strong>情绪标签</strong>：直接情绪标签、心中波澜等</div>
            <div class="pc-item"><strong>句式</strong>：不是而是、破折号等</div>
            <div class="pc-item"><strong>俗套</strong>：面部大忌、身体大忌、四肢百骸等</div>
            <div class="pc-item"><strong>严禁词</strong>：死死等</div>
          </div>
        </section>

        <section class="tutorial-section">
          <h4>注意事项</h4>
          <ul class="tutorial-notes">
            <li>白名单不等于滥用——即使在允许的场景中，也有密度限制</li>
            <li>角色状态锁是防止"记忆漂移"的关键，请确保 Bible 中的角色信息完整</li>
            <li>流式扫描使用文本模式检测，完成时仍会独立全量复扫；章后审计不会被流式扫描替代</li>
            <li>学习服务发现的新模式需要人工审核通过后才会生效</li>
            <li>正向行为映射的核心是"不要写禁止，要写替代"——让模型有明确的方向</li>
            <li>章节审计是全自动的，每次生成章节后自动运行，无需手动触发</li>
          </ul>
        </section>
      </div>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { NButton, NInput, NTag, NModal, NSpin, NEmpty, NSelect, useMessage } from 'naive-ui'
import { scanChapter, getAntiAIStats, getRules, getAllowlistScenes, getAuditRuns, getAuditHistory, getAuditTrend, getStreamRuns, getPromptRuns, getAuditProgress } from '../../../api/anti-ai'
import { novelApi } from '../../../api/novel'
import type { ScanResult, AntiAIStats, AntiAIRule, AllowlistScene, AntiAIAuditRun, AntiAITrendPoint, AntiAIStreamRun, AntiAIPromptRun, AntiAIScanProgress } from '../../../types/anti-ai'
import { ASSESSMENT_COLORS, SCENE_TYPE_LABELS } from '../../../types/anti-ai'
import AntiAIRewriteReview from './AntiAIRewriteReview.vue'

const message = useMessage()
const route = useRoute()
const historyNovelId = ref<string | null>(null)
const novelOptions = ref<{ label: string; value: string }[]>([])
const auditRuns = ref<AntiAIAuditRun[]>([])
const streamRuns = ref<AntiAIStreamRun[]>([])
const promptRuns = ref<AntiAIPromptRun[]>([])
const scanProgress = ref<AntiAIScanProgress[]>([])
const progressError = ref('')
let progressRequest = 0
let progressTimer: ReturnType<typeof setInterval> | null = null

function progressStageLabel(stage: string) {
  return ({ scanning: '正在扫描', persisting: '扫描完成，正在归档', completed: '扫描及归档完成', failed: '扫描或归档失败' } as Record<string, string>)[stage] || stage
}

async function loadProgress() {
  const generation = ++progressRequest
  const novelId = historyNovelId.value
  if (!novelId) { scanProgress.value = []; progressError.value = ''; return }
  try {
    const result = await getAuditProgress(novelId)
    if (generation !== progressRequest) return
    scanProgress.value = result
    progressError.value = ''
  } catch {
    if (generation !== progressRequest) return
    scanProgress.value = []
    progressError.value = '独立扫描进度暂不可用；无法确认本次审计终态。'
  }
}
onMounted(() => {
  progressTimer = setInterval(() => { if (activeSubTab.value === 'history') void loadProgress() }, 3000)
})
onUnmounted(() => { if (progressTimer) clearInterval(progressTimer); ++progressRequest })
const legacyAudits = ref<Record<string, unknown>[]>([])
const trend = ref<AntiAITrendPoint[]>([])
const historyLoading = ref(false)
const historyError = ref('')
const statsError = ref('')
let historyRequest = 0
let statsRequest = 0

function scanStatusLabel(status: string) {
  return ({ completed: '已完成', failed: '失败', unavailable: '不可用', not_run: '未运行' } as Record<string, string>)[status] || status
}

function streamStatusLabel(status: string) {
  return ({ completed: '扫描完成（正文尚需章后审计）', failed: '扫描失败', interrupted: '按阈值停止', cancelled: '已取消，未确认完成' } as Record<string, string>)[status] || status
}

async function loadHistory() {
  const requestId = ++historyRequest
  const novelId = historyNovelId.value
  void loadProgress()
  auditRuns.value = []
  streamRuns.value = []
  promptRuns.value = []
  legacyAudits.value = []
  trend.value = []
  historyError.value = ''
  if (!novelId) { historyLoading.value = false; return }
  historyLoading.value = true
  const results = await Promise.allSettled([getAuditRuns(novelId), getAuditHistory(novelId), getAuditTrend(novelId), getStreamRuns(novelId), getPromptRuns(novelId)])
  if (requestId !== historyRequest) return
  const [runs, legacy, trendResult, streams, prompts] = results
  if (runs.status === 'fulfilled') auditRuns.value = runs.value
  if (legacy.status === 'fulfilled') legacyAudits.value = legacy.value
  if (trendResult.status === 'fulfilled') trend.value = trendResult.value
  if (streams.status === 'fulfilled') streamRuns.value = streams.value
  if (prompts.status === 'fulfilled') promptRuns.value = prompts.value
  if (results.some(result => result.status === 'rejected')) historyError.value = '部分审计记录加载失败，请刷新重试。'
  historyLoading.value = false
}
watch(historyNovelId, () => { scanProgress.value = []; progressError.value = ''; void loadHistory(); void loadStats() })

// 子标签页
const subTabs = [
  { key: 'overview', icon: '', label: '概览' },
  { key: 'scan', icon: '', label: '快速扫描' },
  { key: 'history', icon: '', label: '自动审计记录' },
  { key: 'review', icon: '', label: '改写候选与策略' },
  { key: 'rules', icon: '', label: '规则' },
  { key: 'allowlist', icon: '', label: '白名单' },
]
const activeSubTab = ref('overview')

// 状态
const scanInput = ref('')
const scanning = ref(false)
const scanResult = ref<ScanResult | null>(null)
const antiAIStats = ref<AntiAIStats | null>(null)
const showTutorial = ref(false)

// 规则数据
const rules = ref<AntiAIRule[]>([])
const rulesLoading = ref(false)

// 白名单数据
const allowlistScenes = ref<AllowlistScene[]>([])
const allowlistLoading = ref(false)

// 七层防御定义
const defenseLayers = computed(() => {
  const layers = antiAIStats.value?.layers
  const states = antiAIStats.value?.layer_states || {}
  const layerState = (key: string) => states[key]
  const statusLabel = (key: string, fallback: string) => {
    const state = layerState(key)
    if (!state) return fallback
    if (state.failed) return '执行失败'
    if (state.status === 'unavailable') return state.latest_execution?.reason || '本次不可用'
    if (state.observed) return '已有运行记录'
    if (state.connected) return '已接线，尚未观测'
    if (state.configured) return state.connection_evidence ? '已配置，运行接线未确认' : '已配置，未接线'
    return '仅实现，未配置'
  }
  return [
    {
      key: 'L1',
      name: 'L1 正向行为映射',
      desc: '将否定指令转为确定性的动作映射',
      icon: '',
      color: '#6366f1',
      active: Boolean(layerState('L1_positive_framing')?.observed),
      failed: Boolean(layerState('L1_positive_framing')?.failed),
      status: statusLabel('L1_positive_framing', layers ? '已配置，尚未观测' : '状态未知'),
    },
    {
      key: 'L2',
      name: 'L2 核心协议 P1-P5',
      desc: '五大写作法则：密度/感官/差异/节奏/衔接',
      icon: '',
      color: '#8b5cf6',
      active: Boolean(layerState('L2_protocol_rules')?.observed),
      failed: Boolean(layerState('L2_protocol_rules')?.failed),
      status: statusLabel('L2_protocol_rules', layers ? '已配置，尚未观测' : '状态未知'),
    },
    {
      key: 'L3',
      name: 'L3 场景化白名单',
      desc: '不同场景的差异化模式豁免',
      icon: '',
      color: '#a855f7',
      active: Boolean(layerState('L3_allowlist_scenes')?.observed),
      failed: Boolean(layerState('L3_allowlist_scenes')?.failed),
      status: statusLabel('L3_allowlist_scenes', layers ? '已配置，尚未观测' : '状态未知'),
    },
    {
      key: 'L4',
      name: 'L4 角色状态向量',
      desc: '声线指纹/紧张习惯/反应模式/信息边界',
      icon: '',
      color: '#d946ef',
      active: Boolean(layerState('L4_state_vector')?.observed),
      failed: Boolean(layerState('L4_state_vector')?.failed),
      status: statusLabel('L4_state_vector', layers ? '已接线，尚未观测' : '状态未知'),
    },
    {
      key: 'L5',
      name: 'L5 上下文配额',
      desc: '洋葱模型配额分配，T0 永不压缩',
      icon: '',
      color: '#ec4899',
      active: Boolean(layerState('L5_context_quota')?.observed),
      failed: Boolean(layerState('L5_context_quota')?.failed),
      status: statusLabel('L5_context_quota', layers ? '已接线，尚未观测' : '状态未知'),
    },
    {
      key: 'L6',
      name: 'L6 正文流式扫描',
      desc: '正文流式分块扫描、告警或显式停止，完成时独立复扫',
      icon: '',
      color: '#f43f5e',
      active: Boolean(layerState('L6_token_guard')?.observed),
      failed: Boolean(layerState('L6_token_guard')?.failed),
      status: statusLabel('L6_token_guard', layers ? '已接线，尚未观测' : '状态未知'),
    },
    {
      key: 'L7',
      name: 'L7 章后审计',
      desc: '35+模式检测 + 指标趋势 + 自适应学习',
      icon: '',
      color: '#ef4444',
      active: Boolean(layerState('L7_audit')?.observed),
      failed: Boolean(layerState('L7_audit')?.failed),
      status: statusLabel('L7_audit', layers?.L7_audit === 'observed' ? '已观测到运行记录' : '已接线，尚未观测'),
    },
  ]
})

// 评估颜色
const assessmentColor = computed(() => {
  if (!scanResult.value) return '#6b7280'
  return ASSESSMENT_COLORS[scanResult.value.overall_assessment] || '#6b7280'
})

// 方法
async function handleScan() {
  if (!scanInput.value.trim()) return
  scanning.value = true
  scanResult.value = null
  try {
    scanResult.value = await scanChapter(scanInput.value)
  } catch (e: any) {
    message.error(e?.message || '扫描失败')
  } finally {
    scanning.value = false
  }
}

function severityTagType(severity: string) {
  switch (severity) {
    case 'critical': return 'error'
    case 'warning': return 'warning'
    case 'info': return 'info'
    default: return 'default'
  }
}

function getSceneLabel(sceneType: string): string {
  return SCENE_TYPE_LABELS[sceneType] || sceneType
}

async function loadStats() {
  const requestId = ++statsRequest
  statsError.value = ''
  try {
    const novelId = historyNovelId.value || String(route.params.slug || '')
    const result = await getAntiAIStats(novelId || undefined)
    if (requestId !== statsRequest) return
    antiAIStats.value = result
  } catch {
    if (requestId !== statsRequest) return
    antiAIStats.value = null
    statsError.value = '能力状态暂不可用，请刷新重试。'
  }
}

async function loadRules() {
  rulesLoading.value = true
  try {
    rules.value = await getRules()
  } catch {
    // 静默失败
  } finally {
    rulesLoading.value = false
  }
}

async function loadAllowlist() {
  allowlistLoading.value = true
  try {
    allowlistScenes.value = await getAllowlistScenes()
  } catch {
    // 静默失败
  } finally {
    allowlistLoading.value = false
  }
}

onMounted(async () => {
  loadStats()
  loadRules()
  loadAllowlist()
  try {
    const novels = await novelApi.listNovels()
    novelOptions.value = novels.map(n => ({ label: n.title, value: n.id }))
    const currentId = String(route.params.slug || '')
    if (novels.some(n => n.id === currentId)) historyNovelId.value = currentId
  } catch {
    historyError.value = '小说列表加载失败，请重新打开面板。'
  }
})
</script>

<style scoped>
.anti-ai-dashboard {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
}

.trend-point {
  display: inline-block;
  margin-left: 10px;
  color: var(--app-text-muted);
  font-size: 12px;
}

/* 顶部 */
.dashboard-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.dashboard-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: var(--app-text-primary);
}
.dashboard-subtitle {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--app-text-muted);
}

/* 子标签 */
.sub-tabs {
  display: flex;
  gap: 0;
  border-bottom: 2px solid var(--app-border);
  flex-shrink: 0;
}
.sub-tab {
  font-size: 13px;
  font-weight: 500;
  padding: 8px 16px;
  cursor: pointer;
  color: var(--app-text-muted);
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  transition: all 0.2s ease;
  user-select: none;
}
.sub-tab:hover {
  color: var(--app-text-primary);
}
.sub-tab.is-active {
  color: var(--color-brand);
  border-bottom-color: var(--color-brand);
  font-weight: 600;
}

/* 七层防御 */
.layers-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
}
.layer-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--app-radius-md);
  border: 1px solid var(--app-border);
  background: var(--app-surface);
  transition: all 0.2s ease;
}
.layer-card:hover {
  border-color: var(--layer-color);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
.layer-card.is-active {
  border-left: 3px solid var(--layer-color);
}
.layer-icon {
  font-size: 22px;
  flex-shrink: 0;
}
.layer-info {
  flex: 1;
  min-width: 0;
}
.layer-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-primary);
}
.layer-desc {
  font-size: 11.5px;
  color: var(--app-text-muted);
  margin-top: 2px;
}
.layer-status {
  flex-shrink: 0;
}

/* 扫描区 */
.scan-section,
.rules-section,
.allowlist-section {
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-lg);
  padding: 18px;
  border: 1px solid var(--app-border);
}
.section-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text-primary);
  margin: 0 0 6px;
}
.section-desc {
  font-size: 12.5px;
  color: var(--app-text-muted);
  margin: 0 0 14px;
  line-height: 1.6;
}
.scan-input :deep(textarea) {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
  line-height: 1.6;
}
.scan-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

/* 扫描结果 */
.scan-result {
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid var(--app-border);
}
.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.result-assessment {
  font-size: 24px;
  font-weight: 800;
}
.result-score {
  font-size: 14px;
  color: var(--app-text-secondary);
}

.result-stats {
  display: flex;
  gap: 20px;
  margin-bottom: 16px;
}
.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--app-text-primary);
}
.stat-value.critical { color: #dc2626; }
.stat-value.warning { color: #f59e0b; }
.stat-label {
  font-size: 12px;
  color: var(--app-text-muted);
}

/* 分类分布 */
.sub-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-primary);
  margin: 14px 0 8px;
}
.dist-bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.dist-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dist-cat {
  font-size: 12px;
  color: var(--app-text-secondary);
  width: 60px;
  text-align: right;
  flex-shrink: 0;
}
.dist-bar-bg {
  flex: 1;
  height: 8px;
  background: var(--app-surface);
  border-radius: 4px;
  overflow: hidden;
}
.dist-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--color-brand), var(--color-purple));
  border-radius: 4px;
  transition: width 0.3s ease;
  min-width: 4px;
}
.dist-count {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-primary);
  width: 24px;
  flex-shrink: 0;
}

/* 建议 */
.suggestions,
.recommendations {
  margin-top: 4px;
}
.suggestion-item {
  font-size: 12.5px;
  color: var(--app-text-secondary);
  padding: 8px 12px;
  background: var(--app-surface);
  border-radius: var(--app-radius-sm);
  margin-bottom: 4px;
  border: 1px solid var(--app-border);
  border-left: 3px solid #22c55e;
}
.recommendation-item {
  font-size: 12.5px;
  color: var(--app-text-secondary);
  padding: 8px 12px;
  background: var(--app-surface);
  border-radius: var(--app-radius-sm);
  margin-bottom: 4px;
  border: 1px solid var(--app-border);
  border-left: 3px solid #f59e0b;
}

/* 命中详情 */
.hits-list {
  margin-top: 4px;
}
.hit-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  font-size: 12px;
  border-radius: var(--app-radius-sm);
  margin-bottom: 3px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
}
.hit-item.severity-critical {
  border-left: 3px solid #dc2626;
}
.hit-item.severity-warning {
  border-left: 3px solid #f59e0b;
}
.hit-pattern {
  color: var(--app-text-primary);
  font-weight: 500;
  flex-shrink: 0;
}
.hit-text {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--app-surface-subtle);
  padding: 1px 5px;
  border-radius: 3px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hit-hint {
  color: var(--color-brand);
  font-size: 11px;
  margin-left: auto;
  flex-shrink: 0;
}
.more-hits {
  font-size: 12px;
  color: var(--app-text-muted);
  text-align: center;
  padding: 8px;
}

/* 系统统计 */
.stats-section {
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-lg);
  padding: 18px;
  border: 1px solid var(--app-border);
}
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.stat-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 14px 10px;
  background: var(--app-surface);
  border-radius: var(--app-radius-md);
  border: 1px solid var(--app-border);
}
.stat-number {
  font-size: 22px;
  font-weight: 800;
  color: var(--color-brand);
}
.stat-number.accent {
  color: #d946ef;
}
.stat-desc {
  font-size: 12px;
  color: var(--app-text-muted);
  margin-top: 4px;
}

/* 规则列表 */
.rules-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rule-card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  padding: 12px 14px;
  border-left: 3px solid var(--color-brand);
}
.rule-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.rule-anti {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-primary);
}
.rule-action {
  font-size: 12.5px;
  color: var(--app-text-secondary);
  line-height: 1.5;
}
.rule-label {
  font-weight: 600;
  color: #22c55e;
}

/* 白名单场景 */
.scenes-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.scene-card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  padding: 14px 16px;
}
.scene-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.scene-type-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text-primary);
}
.scene-key {
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--app-text-muted);
  background: var(--app-surface-subtle);
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--app-border);
}
.scene-desc {
  font-size: 12.5px;
  color: var(--app-text-secondary);
  margin: 0 0 10px;
  line-height: 1.5;
}
.scene-categories,
.scene-patterns {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 6px;
}
.scene-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--app-text-muted);
  margin-right: 4px;
}

/* 加载 */
.loading-wrap {
  display: flex;
  justify-content: center;
  padding: 24px;
}

/* 教程 */
.tutorial-content {
  font-size: 14px;
  line-height: 1.7;
  color: var(--app-text-secondary);
}
.tutorial-section {
  margin-bottom: 20px;
}
.tutorial-section h4 {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text-primary);
  margin: 0 0 8px;
}
.tutorial-layers {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tl-item {
  padding: 8px 12px;
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-sm);
  border-left: 3px solid var(--color-brand);
  font-size: 13px;
}
.tutorial-steps {
  padding-left: 20px;
}
.tutorial-steps li {
  margin-bottom: 6px;
}
.tutorial-notes {
  padding-left: 20px;
}
.tutorial-notes li {
  margin-bottom: 4px;
  color: var(--app-text-muted);
}
.tutorial-content code {
  background: var(--app-surface-subtle);
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 12px;
  font-family: var(--font-mono);
}
.pattern-categories {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.pc-item {
  padding: 6px 10px;
  background: var(--app-surface-subtle);
  border-radius: var(--app-radius-sm);
  font-size: 12.5px;
  border-left: 2px solid var(--color-brand);
}
</style>
