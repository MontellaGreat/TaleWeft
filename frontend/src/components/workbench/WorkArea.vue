<!-- Modified for TaleWeft on 2026-10-22: product branding; compatibility identifiers retained. -->
<template>
  <div class="work-area" :style="{ '--manuscript-font-size': `${manuscriptFontSize}px` }">
    <header class="work-header">
      <div class="work-title-wrap">
        <h2 class="work-title">{{ bookTitle || slug }}</h2>
        <n-text depth="3" class="work-sub">{{ currentChapter ? ordinalUnit(currentChapter.number) : '作者书桌' }}</n-text>
      </div>
      <div v-if="!proseOnlyWorkbench" class="work-mode-switch" role="group" aria-label="创作模式">
        <n-switch
          v-model:value="workMode"
          checked-value="managed"
          unchecked-value="assisted"
          size="large"
        >
          <template #unchecked>辅助撰稿</template>
          <template #checked>托管撰稿</template>
        </n-switch>
      </div>
    </header>

    <div class="work-body">
      <!--
        辅助撰稿与托管撰稿须同时保留在 DOM（v-show，勿用 v-if）：
        切到辅助撰稿时若卸载 AutopilotPanel，其 onUnmounted 会 stopChapterStream()，
        章节 SSE 断开会导致全托管写作异常/重连后重复写。
      -->
      <div v-show="workMode === 'assisted'" class="assisted-stack">
        <n-alert
          v-if="isAssistedReadOnly"
          type="warning"
          :show-icon="true"
          class="assisted-readonly-banner"
        >
          <strong>全托管运行中</strong>：本侧仅只读；不能保存、改稿、快速生成或改章节元素。
          请切换到「<strong>托管撰稿</strong>」看驾驶舱与监控，或停止托管后再编辑。
        </n-alert>
        <ChapterWorkbenchShell
          class="chapter-desk-shell"
          :stacked="desk.stacked"
          :rail-enabled="!proseOnlyWorkbench"
          v-model:rail-expanded="desk.railExpanded"
          rail-drawer-title="本章任务与状态"
        >
          <template #manuscript-toolbar>
            <div v-if="!proseOnlyWorkbench" class="desk-toolbar">
              <n-space align="center" :size="8" wrap>
                <template v-if="signalStrip">
                  <n-tag size="small" round type="info">张力 {{ signalStrip.tension }}/10</n-tag>
                  <n-tag v-if="!signalStrip.sync" size="small" round type="warning">叙事未同步</n-tag>
                </template>
                <n-text v-else depth="3" style="font-size: 12px">本章信号在侧栏与生成完成后更新</n-text>
                <template v-if="guardrailSnapshot">
                  <n-tag size="small" round :type="guardrailSnapshot.passed ? 'success' : 'warning'">
                    护栏 {{ guardrailSnapshot.passed ? '已通过' : '待关注' }} · {{ Math.round((guardrailSnapshot.overall_score || 0) * 100) }} 分
                  </n-tag>
                  <n-tag v-if="(guardrailSnapshot.violations?.length || 0) > 0" size="small" round type="warning">
                    {{ guardrailSnapshot.violations?.length }} 条提示
                  </n-tag>
                </template>
              </n-space>
              <n-space v-if="!proseOnlyWorkbench" align="center" :size="6" wrap justify="end">
                <n-button size="tiny" quaternary @click="showGuardrailModal = true">护栏详情</n-button>
                <n-button size="tiny" quaternary @click="showTraceModal = true">引擎溯源</n-button>
                <n-text depth="3" style="font-size: 11px">元素用主栏标签切换</n-text>
                <n-button size="tiny" secondary @click="desk.toggleRail()">
                  {{ desk.railExpanded ? '收起侧栏' : '任务与状态' }}
                </n-button>
              </n-space>
            </div>
          </template>

          <template #primary>
            <div class="work-main primary-desk-root">
              <div v-if="!currentChapter" class="work-empty work-empty-prose">
                <n-empty description="暂无选中章节" class="work-empty-card" />
              </div>
              <n-tabs
                v-else
                v-model:value="primaryDeskTab"
                type="line"
                size="small"
                animated
                class="primary-desk-tabs"
              >
                <n-tab-pane name="manuscript" tab="章节编辑" display-directive="if">
                  <div class="chapter-editor">
                <div class="editor-header">
                  <div class="editor-header-main">
                    <div class="editor-title">
                      <n-input v-model:value="editorTitle" aria-label="章节标题" placeholder="章节标题" :maxlength="200" :disabled="isAssistedReadOnly || loading" style="max-width: 420px" />
                      <n-tag size="small" :type="currentChapter.word_count > 0 ? 'success' : 'default'" round>
                        {{ currentChapter.word_count > 0 ? '已收稿' : '未收稿' }}
                      </n-tag>
                      <n-tag v-if="isAutopilotRunning && streamingChapterNumber === currentChapter.number" size="small" type="info" round>
                        生成中...
                      </n-tag>
                    </div>
                    <n-space v-if="!proseOnlyWorkbench" :size="8" class="editor-header-actions">
                      <n-tooltip trigger="click" placement="bottom-end" :show-arrow="false" style="background: var(--app-surface); color: var(--app-text); border: 1px solid var(--app-border)">
                        <template #trigger><n-button size="small" aria-label="正文阅读设置">字号 {{ manuscriptFontSize }}</n-button></template>
                        <div class="reading-settings">
                          <label for="manuscript-font-size">正文字号（仅影响显示）</label>
                          <n-slider id="manuscript-font-size" v-model:value="manuscriptFontSize" :min="16" :max="24" :step="1" :marks="{ 16: '16', 20: '20', 24: '24' }" />
                        </div>
                      </n-tooltip>
                      <n-button size="small" :disabled="loading || !editorDraft" @click="showHistoryModal = true">历史版本</n-button>
                      <n-button size="small" @click="handleReload" :disabled="loading">重新加载</n-button>
                      <n-button
                        size="small"
                        type="primary"
                        @click="handleSave"
                        :disabled="!hasChanges || isAssistedReadOnly"
                        :loading="saving"
                      >
                        保存
                      </n-button>
                    </n-space>
                  </div>
                  <div v-if="!proseOnlyWorkbench && autopilotStatus?.current_act_title" class="act-info-header">
                    <span class="act-info-title">第 {{ (autopilotStatus.current_act || 0) + 1 }} 幕 · {{ autopilotStatus.current_act_title }}</span>
                    <span v-if="autopilotStatus.current_act_description" class="act-info-desc">{{ autopilotStatus.current_act_description }}</span>
                  </div>
                </div>

                <div class="editor-body">
                  <div
                    class="editor-input-wrapper"
                    :class="{ 'is-streaming': isAutopilotRunning && streamingChapterNumber === currentChapter.number && streamingContent }"
                  >
                    <n-input
                      v-model:value="editorDisplayContent"
                      type="textarea"
                      aria-label="章节正文"
                      placeholder="在这里写下故事，或使用下方按钮生成本章正文。"
                      :autosize="false"
                      :readonly="isAssistedReadOnly || loading"
                      @update:value="handleContentChange"
                    />
                    <div
                      v-if="isAutopilotRunning && streamingChapterNumber === currentChapter.number && streamingContent"
                      class="streaming-cursor-overlay"
                    >
                      <span class="streaming-cursor">▋</span>
                      <span class="streaming-badge">生成中</span>
                    </div>
                  </div>
                </div>

                <div class="editor-footer">
                  <n-space :size="8" align="center" justify="space-between" style="width: 100%">
                    <n-space vertical :size="4" style="min-width: 0">
                    <n-text depth="3" class="editor-wordcount-line">
                      <template
                        v-if="
                          isAutopilotRunning &&
                          streamingChapterNumber === currentChapter?.number &&
                          streamingContent &&
                          streamingWordCountHint
                        "
                      >
                        <span :class="{ 'streaming-word-count': true }">{{ streamingWordCountHint }}</span>
                        <n-tooltip trigger="hover" placement="top">
                          <template #trigger>
                            <span class="wordcount-help">?</span>
                          </template>
                          流式阶段模型常会写出超过单章目标的缓冲；落稿字数会贴近你在书目里设的「每章目标字数」。
                        </n-tooltip>
                        <span class="streaming-indicator">生成中</span>
                      </template>
                      <template v-else>
                        字数:
                        <span :class="{ 'streaming-word-count': isAutopilotRunning && streamingChapterNumber === currentChapter?.number && streamingContent }">
                          {{ wordCount }}
                        </span>
                        <span v-if="isAutopilotRunning && streamingChapterNumber === currentChapter?.number && streamingContent" class="streaming-indicator">生成中▋</span>
                      </template>
                    </n-text>
                    <n-text v-if="!proseOnlyWorkbench" depth="3" style="font-size: 11px; max-width: 56ch; line-height: 1.45">
                      实体标记（可选）：
                      <code>[[char:id|人名]] [[loc:id|地名]] [[faction:id|势力]] [[prop:id|道具]]</code>
                      · 保存后自动索引本章实体，侧栏「手稿道具」可查看。
                    </n-text>
                    </n-space>
                    <n-space :size="8">
                      <n-tooltip trigger="hover" :disabled="proseOnlyWorkbench || (!isAutopilotRunning && !isAssistedReadOnly)">
                        <template #trigger>
                          <n-button
                            size="small"
                            type="primary"
                            @click="handleGenerateChapter"
                            :loading="generating || generateInProgress"
                            :disabled="proseOnlyWorkbench ? generateInProgress : (isAutopilotRunning || isAssistedReadOnly)"
                          >
                            {{ prosePrimaryActionLabel }}
                          </n-button>
                        </template>
                        <span>{{ isAssistedReadOnly ? '托管运行中不可手动生成' : 'Autopilot 运行时禁用手动生成' }}</span>
                      </n-tooltip>
                      <n-tooltip
                        v-if="hasChapterContent"
                        trigger="hover"
                        :disabled="!isAutopilotRunning && !isAssistedReadOnly"
                        :content="isAssistedReadOnly ? '托管运行中不可重新生成' : 'Autopilot 运行时禁用'"
                      >
                        <template #trigger>
                          <n-button
                            size="small"
                            secondary
                            @click="handleRegenerateChapter"
                            :loading="generating"
                            :disabled="isAutopilotRunning || isAssistedReadOnly"
                          >
                            🔄 重新生成
                          </n-button>
                        </template>
                      </n-tooltip>
                      <n-button v-if="!proseOnlyWorkbench" size="small" secondary :disabled="isAssistedReadOnly" @click="openTensionModal" title="诊断当前章节张力缺口">
                        🔍 张力诊断
                      </n-button>
                    </n-space>
                  </n-space>
                </div>
                  </div>
                </n-tab-pane>

                <n-tab-pane v-if="!proseOnlyWorkbench" name="elements" tab="本章舞台" display-directive="if">
                  <div class="elements-tab-wrap primary-tab-pane">
                    <ChapterElementPanel
                      :slug="slug"
                      :current-chapter-number="currentChapter.number"
                      :read-only="isAssistedReadOnly"
                      :last-workflow-result="lastWorkflowResult"
                      :qc-chapter-number="lastQcChapterNumber"
                      :autopilot-chapter-review="autopilotChapterReview"
                    />
                  </div>
                </n-tab-pane>
              </n-tabs>
            </div>
          </template>

          <template #rail>
            <div v-if="!proseOnlyWorkbench" class="rail-column">
              <div class="rail-head">
                <n-text strong style="font-size: 13px">本章任务与状态</n-text>
                <n-button v-if="!desk.stacked" quaternary circle size="small" @click="desk.toggleRail()" title="收起侧栏">
                  <template #icon>
                    <ChevronForwardOutline />
                  </template>
                </n-button>
              </div>
              <!-- 写作流（始终置顶，覆盖两个 tab） -->
              <AutopilotWritingStream
                v-if="isAutopilotRunning && (streamingContent || isAutopilotWriting)"
                class="wa-inline-stream rail-stream-bar"
                :writing-content="streamingContent"
                :writing-chapter-number="streamingChapterNumber ?? undefined"
                :writing-substep="String(autopilotStatus?.writing_substep || '')"
                :writing-substep-label="String(autopilotStatus?.writing_substep_label || '')"
                :accumulated-words="Number(autopilotStatus?.accumulated_words || 0)"
                :chapter-target-words="Number(autopilotStatus?.chapter_target_words || 0)"
                :is-writing-phase="isAutopilotWriting"
                :status-chapter-number="streamingChapterNumber"
              />
              <!-- 2-tab: 规划 / 状态 -->
              <n-tabs
                v-model:value="railActiveTab"
                type="line"
                size="small"
                class="rail-tabs"
                :tabs-padding="4"
              >
                <n-tab-pane name="plan" tab="规划" display-directive="if">
                  <n-scrollbar class="rail-tab-scroll">
                    <div class="rail-scroll-pad">
                      <ChapterContentPanel
                        :slug="slug"
                        :current-chapter-number="currentChapter?.number ?? null"
                        :read-only="isAssistedReadOnly"
                        :autopilot-chapter-review="autopilotChapterReview"
                        :assist-stream-beat-session="railAssistBeatSession"
                        :assist-stream-failed-chapter="assistStreamFailedChapter"
                        :assist-stream-plan-failed-chapter="assistStreamPlanFailedChapter"
                        :autopilot-outline-plan-failed="autopilotOutlinePlanFailedForRail"
                        :outline-plan-mode="autopilotOutlinePlanModeForRail"
                        :assist-stream-completed-chapter="lastQcChapterNumber"
                        :beat-tab-bump="beatTabBump"
                      />
                    </div>
                  </n-scrollbar>
                </n-tab-pane>
                <n-tab-pane name="status" tab="状态" display-directive="if">
                  <n-scrollbar class="rail-tab-scroll">
                    <div class="rail-scroll-pad">
                      <ChapterStatusPanel
                        :slug="slug"
                        :chapter="currentChapter"
                        :read-only="isAssistedReadOnly"
                        :last-workflow-result="lastWorkflowResult"
                        :qc-chapter-number="lastQcChapterNumber"
                        :autopilot-chapter-review="autopilotChapterReview"
                        @clear-qc="clearWorkflowQc"
                        @go-editor="focusManuscriptEditor"
                      />
                    </div>
                  </n-scrollbar>
                </n-tab-pane>
              </n-tabs>
            </div>
          </template>

          <template #rail-collapsed-actions>
            <n-tooltip v-for="id in CHAPTER_DESK_AUX_ORDER" :key="id" placement="left" trigger="hover">
              <template #trigger>
                <n-button quaternary size="small" class="rail-icon-btn" @click="primaryDeskTab = id">
                  <template #icon>
                    <component :is="auxPaneIcon(id)" />
                  </template>
                </n-button>
              </template>
              {{ CHAPTER_DESK_AUX_SURFACES[id].label }}
            </n-tooltip>
          </template>
        </ChapterWorkbenchShell>
      </div>

      <!-- 托管撰稿：驾驶舱 / 仪表盘 / 监控·DAG；组件内 v-show 保持 SSE -->
      <AutopilotWorkspace
        v-if="!proseOnlyWorkbench"
        v-show="workMode === 'managed'"
        class="managed-stack"
        :novel-id="slug"
        :cockpit-visible="workMode === 'managed'"
        @status-change="handleAutopilotStatusChange"
        @chapter-content-update="handleChapterContentUpdate"
        @chapter-chunk="handleChapterChunkStream"
        @desk-refresh="handleAutopilotDeskRefreshFromStream"
        @beats-planned="handleAutopilotBeatsPlanned"
      />

    </div>

    <!-- AI 生成本章弹窗（流式 + 质检结果在「章节状态」） -->
    <n-modal
      v-if="!proseOnlyWorkbench"
      v-model:show="showGenerateModal"
      preset="card"
      :title="isRegenerationMode ? '🔄 重新生成本章' : 'AI 生成本章（含一致性检查）'"
      style="width: min(820px, 96vw); max-height: min(92vh, 900px)"
      :segmented="{ content: true, footer: 'soft' }"
      :mask-closable="!generateInProgress"
    >
      <template #header-extra>
        <n-text depth="3" style="font-size: 12px">
          {{ isRegenerationMode ? '原内容将自动快照保存，可从历史草稿恢复' : '同一流式接口；报告在本章侧栏' }}
        </n-text>
      </template>

      <n-scrollbar style="max-height: min(78vh, 760px)">
        <n-space vertical :size="20">
          <n-alert type="info" :show-icon="true">
            选择目标章节与大纲后流式生成。一致性报告与俗套句式命中会出现在右侧「本章任务与状态」侧栏；此处可审阅正文并保存到所选章节。
          </n-alert>

          <n-card title="配置" size="small" :bordered="false">
            <n-space vertical :size="16">
              <n-form-item label="目标章节" label-placement="left" label-width="80">
                <n-select
                  v-model:value="generateTargetChapterId"
                  :options="chapterSelectOptions"
                  placeholder="选择要生成的章节"
                  :disabled="generateInProgress"
                  filterable
                />
              </n-form-item>

              <n-form-item label-placement="left" label-width="80" :show-feedback="false">
                <template #label>
                  <n-space :size="6" align="center">
                    <span>大纲</span>
                    <n-tag v-if="outlineBlurAnalyzing" size="tiny" type="info" round>
                      场景预分析中…
                    </n-tag>
                    <n-tag v-else-if="blurSceneCache" size="tiny" type="success" round>
                      已预分析
                    </n-tag>
                  </n-space>
                </template>
                <n-input
                  v-model:value="generateOutline"
                  type="textarea"
                  placeholder="输入大纲（可选，留空则使用默认）；失焦后自动预分析场景（供生成时复用）"
                  :autosize="{ minRows: 3, maxRows: 8 }"
                  :disabled="generateInProgress"
                  @blur="onOutlineBlurAnalyze"
                />
              </n-form-item>

              <n-form-item label="场记分析" label-placement="left" label-width="80" :show-feedback="false">
                <n-space align="center" :size="8">
                  <n-switch v-model:value="useSceneDirector" :disabled="generateInProgress" size="small" />
                  <n-text depth="3" style="font-size: 12px">
                    若失焦未预分析，则在点击生成时再分析场景（与预分析二选一即可）
                  </n-text>
                </n-space>
              </n-form-item>

              <n-alert v-if="sceneDirectorError" type="warning" :show-icon="true" style="font-size: 12px">
                场记分析失败（不影响生成）：{{ sceneDirectorError }}
              </n-alert>

              <!-- 重新生成模式：改进方向输入 -->
              <template v-if="isRegenerationMode">
                <n-alert type="warning" :show-icon="true" style="font-size: 12px">
                  重新生成将覆盖现有正文。点击「开始生成」前，原内容会自动保存为历史草稿。
                </n-alert>
                <n-form-item label="改进方向" label-placement="left" label-width="80" :show-feedback="false">
                  <n-input
                    v-model:value="regenerationGuidance"
                    type="textarea"
                    placeholder="（可选）告诉 AI 哪里需要改进，例如：增强冲突张力、修复角色前后矛盾、改写结尾悬念..."
                    :autosize="{ minRows: 2, maxRows: 5 }"
                    :disabled="generateInProgress"
                    :maxlength="2000"
                    show-count
                  />
                </n-form-item>
              </template>

              <!-- AI 设置 · 模型档案与提示词 -->
              <n-collapse class="gen-ai-settings-collapse">
                <n-collapse-item name="ai-settings">
                  <template #header>
                    <n-space align="center" :size="6">
                      <span style="font-size:13px;font-weight:600">AI 设置</span>
                      <n-tag size="tiny" round type="info">模型档案与提示词</n-tag>
                    </n-space>
                  </template>
                  <n-space vertical :size="16">
                    <n-form-item label="LLM 配置档案" label-placement="left" label-width="80">
                      <n-select
                        v-model:value="generateProfileId"
                        :options="llmProfileOptions"
                        placeholder="使用系统默认激活档案"
                        :disabled="generateInProgress"
                        clearable
                        :loading="llmProfilesLoading"
                      />
                      <n-text depth="3" style="font-size: 11px">
                        选择特定模型档案；留空则使用系统默认激活档案
                      </n-text>
                    </n-form-item>

                    <n-collapse>
                      <n-collapse-item name="script-prompt" title="六模块剧本提示词">
                        <n-space vertical :size="12">
                          <n-switch v-model:value="useCustomScriptPrompt" :disabled="generateInProgress" size="small">
                            使用自定义模板
                          </n-switch>
                          <template v-if="useCustomScriptPrompt">
                            <n-input
                              v-model:value="customScriptTemplate"
                              type="textarea"
                              placeholder="自定义剧本生成提示词，使用 {变量名} 占位符..."
                              :autosize="{ minRows: 4, maxRows: 10 }"
                              :disabled="generateInProgress"
                            />
                            <n-text depth="3" style="font-size: 11px">
                              变量（对应模板中的 <code v-pre>{<!-- -->{变量名}}</code>）
                            </n-text>
                            <n-dynamic-input
                              v-model:value="scriptPromptVarPairs"
                              preset="pair"
                              key-placeholder="变量名"
                              value-placeholder="值"
                              :disabled="generateInProgress"
                            />
                          </template>
                        </n-space>
                      </n-collapse-item>

                      <n-collapse-item name="prose-prompt" title="剧本转正文提示词">
                        <n-space vertical :size="12">
                          <n-switch v-model:value="useCustomProsePrompt" :disabled="generateInProgress" size="small">
                            使用自定义模板
                          </n-switch>
                          <template v-if="useCustomProsePrompt">
                            <n-input
                              v-model:value="customProseTemplate"
                              type="textarea"
                              placeholder="自定义正文生成提示词，使用 {变量名} 占位符..."
                              :autosize="{ minRows: 4, maxRows: 10 }"
                              :disabled="generateInProgress"
                            />
                            <n-text depth="3" style="font-size: 11px">
                              变量（对应模板中的 <code v-pre>{<!-- -->{变量名}}</code>）
                            </n-text>
                            <n-dynamic-input
                              v-model:value="prosePromptVarPairs"
                              preset="pair"
                              key-placeholder="变量名"
                              value-placeholder="值"
                              :disabled="generateInProgress"
                            />
                          </template>
                        </n-space>
                      </n-collapse-item>
                    </n-collapse>
                  </n-space>
                </n-collapse-item>
              </n-collapse>

              <n-button
                :type="isRegenerationMode ? 'warning' : 'primary'"
                @click="handleStartGenerate"
                :loading="generateInProgress || savingDraftBeforeRegen"
                :disabled="generateInProgress || savingDraftBeforeRegen || isAssistedReadOnly || generateTargetChapterId == null"
                size="medium"
                block
              >
                {{
                  savingDraftBeforeRegen
                    ? '快照原内容…'
                    : generateInProgress
                      ? analyzingScene
                        ? '分析场景中...'
                        : '生成中...'
                      : isRegenerationMode
                        ? '🔄 开始重新生成'
                        : '开始生成'
                }}
              </n-button>
            </n-space>
          </n-card>

          <!-- 上下文预览 -->
          <n-card size="small" :bordered="false">
            <template #header>
              <n-space align="center" justify="space-between" style="width:100%">
                <n-space align="center" :size="6">
                  <span style="font-size:13px;font-weight:600">上下文预览</span>
                  <n-text depth="3" style="font-size:11px">AI 实际接收到的三层信息</n-text>
                </n-space>
                <n-button
                  size="tiny"
                  secondary
                  :loading="loadingContext"
                  @click="previewContext"
                >
                  {{ contextPreview ? '重新获取' : '预览' }}
                </n-button>
              </n-space>
            </template>
            <template v-if="contextPreview && modalTargetChapter">
              <!-- 上下文预算分布 -->
              <n-space vertical :size="8">
                <n-space :size="6" wrap>
                  <n-tag size="small" type="info" round>
                    L1 核心 {{ contextPreview.token_usage.layer1 }}
                  </n-tag>
                  <n-tag size="small" type="success" round>
                    L2 近期 {{ contextPreview.token_usage.layer2 }}
                  </n-tag>
                  <n-tag size="small" type="warning" round>
                    L3 检索 {{ contextPreview.token_usage.layer3 }}
                  </n-tag>
                  <n-tag size="small" round>
                    预算 {{ contextPreview.token_usage.total }} / {{ contextPreview.token_usage.limit }}
                  </n-tag>
                </n-space>
                <n-text depth="3" style="font-size:12px">
                  {{ contextPreview.budget_measure === 'utf8_upper_bound'
                    ? '当前按保守上界分配预算，实际 token 用量以模型回执为准。'
                    : '此处显示上下文预算，实际 token 用量以模型回执为准。' }}
                </n-text>
                <n-progress
                  v-if="contextPreview.token_usage.limit > 0"
                  type="line"
                  :percentage="Math.min(100, Math.round(contextPreview.token_usage.total / contextPreview.token_usage.limit * 100))"
                  :height="6"
                  :border-radius="4"
                  :show-indicator="false"
                  :color="contextPreview.token_usage.total / contextPreview.token_usage.limit > 0.9 ? '#f0a020' : '#18a058'"
                />
                <n-collapse>
                  <n-collapse-item title="Layer 1 · 核心设定（Bible + 伏笔）" name="l1">
                    <n-code :code="contextPreview.layer1.content" word-wrap style="font-size:11px;max-height:200px;overflow:auto" />
                  </n-collapse-item>
                  <n-collapse-item title="Layer 2 · 近期章节与相关设定" name="l2">
                    <n-code :code="contextPreview.layer2.content || '（没有相关上下文）'" word-wrap style="font-size:11px;max-height:200px;overflow:auto" />
                  </n-collapse-item>
                  <n-collapse-item title="Layer 3 · 向量召回" name="l3">
                    <n-code :code="contextPreview.layer3.content" word-wrap style="font-size:11px;max-height:200px;overflow:auto" />
                  </n-collapse-item>
                </n-collapse>
              </n-space>
            </template>
            <n-text v-else depth="3" style="font-size:12px">
              点击「预览」查看上下文内容及预算分布。
            </n-text>
          </n-card>

          <n-card
            v-if="generateInProgress || generatedContent || generatedStatus === 'disconnected' || generatedStatus === 'cancel_unknown'"
            title="生成内容"
            size="small"
            :bordered="false"
          >
            <template #header-extra>
              <n-space :size="8">
                <n-button
                  v-if="generatedContent && !generateInProgress"
                  size="tiny"
                  type="primary"
                  :disabled="isAssistedReadOnly"
                  @click="handleSaveGenerated"
                  :loading="saving"
                >
                  {{ generatedStatus === 'completed' ? '保存到所选章节' : '保存已有片段到所选章节' }}
                </n-button>
                <n-button v-if="generatedStatus === 'disconnected' && !generateInProgress" size="tiny" @click="resumeLastGeneration">恢复本次生成</n-button>
                <n-button v-if="generatedStatus === 'cancel_unknown'" size="tiny" @click="stopGenerate()">重试停止</n-button>
                <n-button
                  size="tiny"
                  @click="clearGeneratedDraft"
                  :disabled="generateInProgress"
                >
                  清空
                </n-button>
              </n-space>
            </template>
            <n-alert v-if="generatedStatus !== 'idle' && generatedStatus !== 'running'" :type="generatedStatus === 'completed' ? 'success' : 'warning'" :show-icon="false">
              {{ streamPhaseLabel }}
              <template v-if="generatedStatus === 'disconnected' || generatedStatus === 'cancel_unknown'">；已有片段暂时只读，确认终态后可编辑，避免恢复事件覆盖本地改动。</template>
            </n-alert>
            <n-space v-if="generateInProgress" vertical :size="8" style="width: 100%">
              <n-progress
                type="line"
                :percentage="streamProgressPct"
                :processing="streamProgressPct < 100"
                :height="8"
                indicator-placement="inside"
              />
              <n-space justify="space-between" style="width: 100%">
                <n-text depth="3" style="font-size: 12px">{{ streamPhaseLabel || '准备中…' }}</n-text>
                <n-text depth="3" style="font-size: 12px">
                  {{ streamStats.chars }} 字 · ~{{ streamStats.estimated_tokens }} tokens
                </n-text>
              </n-space>
            </n-space>

            <!-- SSE 实时日志 + 执行剧本准备骨架 -->
            <n-card v-if="generateInProgress" size="small" bordered class="gen-stream-meta-card">
              <template #header>
                <n-space justify="space-between" align="center" style="width: 100%">
                  <n-text strong style="font-size: 13px">实时日志 · SSE</n-text>
                  <n-text depth="3" style="font-size: 11px">
                    {{ generateSseLog.length }} / {{ MAX_SSE_LOG_LINES }} 条
                  </n-text>
                </n-space>
              </template>
              <n-space vertical :size="10">
                <div v-if="planningSkeletonRows > 0">
                  <n-text depth="3" style="font-size: 11px; display: block; margin-bottom: 8px">
                    章节执行剧本准备中
                  </n-text>
                  <div
                    v-for="i in planningSkeletonRows"
                    :key="'plan-sk-' + i"
                    class="plan-skel-line"
                  >
                    <n-skeleton height="14px" round :style="{ width: planningSkeletonWidthPct(i) }" />
                  </div>
                </div>
                <div>
                  <n-text depth="3" style="font-size: 11px; display: block; margin-bottom: 6px">
                    事件流
                  </n-text>
                  <div ref="sseLogScrollEl" class="sse-log-scroll">
                    <n-space vertical :size="6">
                      <div v-for="(line, idx) in generateSseLog" :key="idx" class="sse-log-row">
                        <n-tag size="tiny" round :type="sseTagType(line.tag)">{{ line.tag }}</n-tag>
                        <n-text style="font-size: 11px; margin-left: 8px" depth="2">{{ line.msg }}</n-text>
                      </div>
                      <n-text v-if="generateSseLog.length === 0" depth="3" style="font-size: 11px">
                        等待 SSE…
                      </n-text>
                    </n-space>
                  </div>
                  <n-button
                    v-if="generateSseLog.length > 0"
                    size="tiny"
                    quaternary
                    block
                    style="margin-top: 8px"
                    @click="scrollGenerateSseLogBottom()"
                  >
                    回到底部
                  </n-button>
                </div>
              </n-space>
            </n-card>

            <n-scrollbar style="max-height: 500px">
              <n-input
                v-model:value="generatedContent"
                type="textarea"
                :autosize="{ minRows: 15, maxRows: 30 }"
                :readonly="generateInProgress || generatedStatus === 'disconnected' || generatedStatus === 'cancel_unknown'"
                placeholder="生成的内容将在此显示..."
              />
            </n-scrollbar>
          </n-card>
        </n-space>
      </n-scrollbar>

      <template #footer>
        <n-space justify="end">
          <n-button @click="showGenerateModal = false" :disabled="generateInProgress">关闭</n-button>
          <n-button v-if="generateInProgress" secondary @click="stopGenerate">停止</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 张力诊断弹窗 -->
    <n-modal
      v-model:show="showTensionModal"
      preset="card"
      title="🔍 张力诊断"
      style="width: min(560px, 96vw)"
    >
      <n-space vertical :size="16">
        <n-alert type="info" :show-icon="false" style="font-size:13px">
          诊断当前章节张力缺口，识别缺失元素并给出突破建议。
        </n-alert>

        <n-form-item label="问题描述（可选）" label-placement="top" :show-feedback="false">
          <n-input
            v-model:value="tensionStuckReason"
            type="textarea"
            placeholder="例：人物对话没有冲突，场景推进感觉平淡……（留空也可分析）"
            :autosize="{ minRows: 2, maxRows: 5 }"
          />
        </n-form-item>

        <n-button type="primary" block :loading="tensionLoading" @click="runTensionSlingshot">
          开始分析
        </n-button>

        <template v-if="tensionResult">
          <n-divider style="margin:4px 0" />
          <n-space vertical :size="10">
            <n-space align="center" :size="8">
              <n-text strong>张力等级</n-text>
              <n-tag
                :type="tensionResult.tension_level === 'high' ? 'success' : tensionResult.tension_level === 'medium' ? 'warning' : 'error'"
                round
              >
                {{ tensionResult.tension_level === 'high' ? '高张力' : tensionResult.tension_level === 'medium' ? '中等' : '低张力 ⚠' }}
              </n-tag>
            </n-space>

            <div>
              <n-text strong style="display:block;margin-bottom:6px">诊断</n-text>
              <n-text style="font-size:13px;line-height:1.7">{{ tensionResult.diagnosis }}</n-text>
            </div>

            <div v-if="tensionResult.missing_elements.length">
              <n-text strong style="display:block;margin-bottom:6px">缺失元素</n-text>
              <n-space wrap :size="6">
                <n-tag v-for="el in tensionResult.missing_elements" :key="el" type="warning" size="small" round>
                  {{ el }}
                </n-tag>
              </n-space>
            </div>

            <div v-if="tensionResult.suggestions.length">
              <n-text strong style="display:block;margin-bottom:6px">突破建议</n-text>
              <n-space vertical :size="6">
                <n-card
                  v-for="(s, i) in tensionResult.suggestions"
                  :key="i"
                  size="small"
                  :bordered="true"
                  style="font-size:13px;line-height:1.7"
                >
                  {{ i + 1 }}. {{ s }}
                </n-card>
              </n-space>
            </div>
          </n-space>
        </template>
      </n-space>
      <template #action>
        <n-space justify="end">
          <n-button @click="showTensionModal = false">关闭</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal
      v-model:show="showGuardrailModal"
      preset="card"
      title="质量护栏（保存后自动）"
      style="width: min(640px, 96vw); max-height: min(88vh, 820px)"
      :segmented="{ content: true }"
    >
      <n-scrollbar style="max-height: min(72vh, 680px)">
        <QualityGuardrailPanel
          v-if="showGuardrailModal && currentChapter"
          :slug="slug"
          :chapter="currentChapter"
          :read-only="isAssistedReadOnly"
        />
      </n-scrollbar>
    </n-modal>

    <n-modal
      v-model:show="showTraceModal"
      preset="card"
      title="引擎溯源"
      style="width: min(720px, 96vw); max-height: min(88vh, 820px)"
    >
      <n-scrollbar style="max-height: min(76vh, 700px)">
        <TraceRecordPanel v-if="showTraceModal" :slug="slug" />
      </n-scrollbar>
    </n-modal>

    <ChapterHistoryModal
      v-model:show="showHistoryModal"
      :slug="editorDraft?.slug || slug"
      :chapter-number="editorDraft?.chapterNumber ?? null"
      :current-version="editorDraft?.version ?? null"
      :allow-restore="!isAssistedReadOnly"
      :before-restore="flushPendingEditor"
      @restored="handleHistoryRestored"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, triggerRef, h, watch, computed, nextTick, onBeforeUnmount, defineAsyncComponent, type Component } from 'vue'
import { storeToRefs } from 'pinia'
import { useUpdateSaveBarrier } from '../../lifecycle/updatePreparation'
import { useDialog, useMessage } from 'naive-ui'
import type { AutopilotStatus } from '../../api/autopilot'
import {
  consumeGenerateChapterStream,
  cancelChapterGeneration,
  analyzeScene,
  retrieveContext,
  saveChapterDraft,
  parseStreamGeneratedBeats,
} from '../../api/workflow'
import type { ContextPreviewResult, GenerateChapterWorkflowResponse, GenerateChapterWithContextPayload, GenerationTerminalStatus, StreamGeneratedBeat } from '../../api/workflow'
import type { GenerationPrefsDTO } from '@/api/novel'
import { chapterApi, getChapterRevisionConflict, type ChapterVersion, type ChapterDTO } from '../../api/chapter'
import ChapterHistoryModal from './ChapterHistoryModal.vue'
import { ChapterDraftSession, rememberChapterDraft, restoreChapterDraft } from '../../workbench/chapterDraftSession'
import { aiInvocationApi } from '../../api/aiInvocation'
import { llmControlApi, type LLMProfile } from '../../api/llmControl'
import { tensionApi } from '../../api/tools'
import type { TensionDiagnosis } from '../../api/tools'
import ChapterWorkbenchShell from './ChapterWorkbenchShell.vue'
import { useStorage } from '@vueuse/core'
import { useChapterDeskLayout } from '../../composables/useChapterDeskLayout'
import { useDebouncedTask } from '../../composables/useDebouncedTask'
import { useWorkbenchRefreshStore } from '../../stores/workbenchRefreshStore'
import { useAIInvocationStore } from '../../stores/aiInvocationStore'
import { featureFlags } from '../../config/features'
import { runtimePerformance } from '../../config/performance'
import {
  CHAPTER_DESK_AUX_ORDER,
  CHAPTER_DESK_AUX_SURFACES,
  type ChapterDeskAuxPaneId,
  type PrimaryChapterDeskTab,
} from '../../workbench/chapterDeskSurface'
import {
  buildAutopilotDeskSnapshot,
  buildAutopilotReactiveFingerprint,
  isAutopilotAfterOutlinePlanSubstep,
  toChapterMicroBeatPayloads,
} from '../../workbench/autopilotStatus'
import { useAssistedAutopilotStatus } from '../../workbench/useAssistedAutopilotStatus'
import { useChapterGuardrailSnapshot } from '../../workbench/useChapterGuardrailSnapshot'
import {
  GENERATE_STREAM_LOG_LIMIT,
  appendGenerateStreamLog,
  generateStreamTagType,
  planningSkeletonWidthPct,
  streamPhaseToLabel,
  streamPhaseToLogLabel,
  streamPhaseToProgress,
  type GenerateStreamLogLine,
} from '../../workbench/generationStreamPresentation'
import {
  getNextProseChapterNumber,
  getProsePrimaryActionLabel,
  hasEditableChapterContent,
  selectProsePrimaryGenerationTarget,
  type ProseGenerationChapterTarget,
} from '../../workbench/chapterGenerationTarget'
import { narrativeOrdinalLabel } from '@/utils/narrativeUnitLabel'
import { loadAssistBeatSession, persistAssistBeatSession } from '@/utils/assistBeatSession'
import { formatApiError, getHttpStatus } from '@/utils/apiError'
import { AppsOutline, ChevronForwardOutline } from '@vicons/ionicons5'

const ChapterContentPanel = defineAsyncComponent(() => import('./ChapterContentPanel.vue'))
const ChapterElementPanel = defineAsyncComponent(() => import('./ChapterElementPanel.vue'))
const ChapterStatusPanel = defineAsyncComponent(() => import('./ChapterStatusPanel.vue'))
const QualityGuardrailPanel = defineAsyncComponent(() => import('./QualityGuardrailPanel.vue'))
const TraceRecordPanel = defineAsyncComponent(() => import('./TraceRecordPanel.vue'))
const AutopilotWorkspace = defineAsyncComponent(() => import('../autopilot/AutopilotWorkspace.vue'))
const AutopilotWritingStream = defineAsyncComponent(() => import('../autopilot/AutopilotWritingStream.vue'))

interface Chapter {
  id: number
  number: number
  title: string
  word_count: number
  content?: string
  revision: number
  content_hash: string
  etag: string
}

interface WorkAreaProps {
  slug: string
  bookTitle?: string
  chapters: Chapter[]
  currentChapterId?: number | null
  chapterContent?: string
  chapterVersion?: ChapterVersion | null
  chapterLoading?: boolean
  generationPrefs?: GenerationPrefsDTO | null
}

const props = withDefaults(defineProps<WorkAreaProps>(), {
  chapters: () => [],
  currentChapterId: null,
  chapterContent: '',
  chapterVersion: null,
  chapterLoading: false,
  generationPrefs: null,
})

function ordinalUnit(n: number) {
  return narrativeOrdinalLabel(n, props.generationPrefs ?? undefined)
}

const emit = defineEmits<{
  chapterUpdated: []
  selectChapter: [chapterNumber: number, title?: string]
}>()

const message = useMessage()
const dialog = useDialog()

const desk = useChapterDeskLayout()
const manuscriptFontSize = useStorage('plotpilot.chapterDesk.fontSize', 18)

const workbenchRefresh = useWorkbenchRefreshStore()
const { deskTick } = storeToRefs(workbenchRefresh)
const aiInvocationStore = useAIInvocationStore()
const managedWorkbenchEnabled = true
const proseOnlyWorkbench = !managedWorkbenchEnabled

const primaryDeskTab = ref<PrimaryChapterDeskTab>('manuscript')
const railActiveTab = ref<'plan' | 'status'>('plan')
const showGuardrailModal = ref(false)
const showTraceModal = ref(false)
const showHistoryModal = ref(false)

function focusManuscriptEditor() {
  desk.focusManuscript()
  primaryDeskTab.value = 'manuscript'
}

watch(
  () => props.currentChapterId,
  () => {
    primaryDeskTab.value = 'manuscript'
  }
)

function auxPaneIcon(id: ChapterDeskAuxPaneId): Component {
  const map: Record<ChapterDeskAuxPaneId, Component> = {
    elements: AppsOutline,
  }
  return map[id]
}

/** 辅助撰稿：编辑与章级工具；托管撰稿：驾驶舱 + 监控大盘 */
const workMode = ref<'assisted' | 'managed'>('assisted')

const showGenerateModal = ref(false)
const generateOutline = ref('')
const generatedContent = ref('')
/** 弹窗内选中的目标章节（与 useWorkbench 映射一致：id === number） */
const generateTargetChapterId = ref<number | null>(null)
const generateInProgress = ref(false)
const lastWorkflowResult = ref<GenerateChapterWorkflowResponse | null>(null)
const lastQcChapterNumber = ref<number | null>(null)
const blurSceneCache = ref<Record<string, unknown> | undefined>(undefined)
const outlineBlurAnalyzing = ref(false)
const streamPhaseLabel = ref('')
const streamProgressPct = ref(0)
const streamStats = ref({ chars: 0, estimated_tokens: 0, chunks: 0 })

/** 辅助撰稿旧链路 · 流式生成下发的指挥器节拍 */
const assistStreamBeatSession = ref<{ chapterNumber: number; beats: StreamGeneratedBeat[] } | null>(null)
/** 旧链路：对应章节流式调用失败 */
const assistStreamFailedChapter = ref<number | null>(null)
/** 流式完成但章前拆拍失败或仅 1 拍（降级） */
const assistStreamPlanFailedChapter = ref<number | null>(null)

// ── Chapter prose invocation ──
const generateProfileId = ref<string | null>(null)
const llmProfiles = ref<LLMProfile[]>([])
const llmProfilesLoading = ref(false)

const useCustomScriptPrompt = ref(false)
const customScriptTemplate = ref('')
const scriptPromptVarPairs = ref<Array<{ key: string; value: string }>>([])

const useCustomProsePrompt = ref(false)
const customProseTemplate = ref('')
const prosePromptVarPairs = ref<Array<{ key: string; value: string }>>([])

const autopilotOutlinePlanFailedForRail = computed(() => {
  const ch = currentChapter.value?.number
  if (!ch || !isAutopilotRunning.value) return false
  const st = autopilotStatus.value
  if (!st) return false
  if (Number(st.current_chapter_number) !== ch) return false
  const sub = String(st.writing_substep ?? '')
  if (!isAutopilotAfterOutlinePlanSubstep(sub)) return false
  return true
})

const autopilotOutlinePlanModeForRail = computed(() => {
  const ch = currentChapter.value?.number
  const st = autopilotStatus.value
  if (!ch || !st) return ''
  if (Number(st.current_chapter_number) !== ch && !isAutopilotRunning.value) return ''
  return String(st.outline_plan_mode ?? '').trim()
})

/** 旧流式节拍缓存只服务历史手动链路；全托管主链不再写 planned_micro_beats */
const autopilotPlannedBeatSession = computed(() => {
  const ch = currentChapter.value?.number
  if (!ch) return null
  const cached = loadAssistBeatSession(props.slug, ch)
  if (cached?.length) return { chapterNumber: ch, beats: cached }
  const st = autopilotStatus.value
  if (!st) return null
  const raw = st.planned_micro_beats
  if (!Array.isArray(raw) || raw.length === 0) return null
  const beats = parseStreamGeneratedBeats(raw)
  if (!beats.length) return null
  if (Number(st.current_chapter_number) !== ch && !isAutopilotRunning.value) return null
  return { chapterNumber: ch, beats }
})

function syncPlannedBeatsFromAutopilotStatus(status: Record<string, unknown> | null | undefined) {
  if (!status) return
  const ch = Number(status.current_chapter_number)
  if (!Number.isFinite(ch) || ch < 1) return
  const raw = status.planned_micro_beats
  if (!Array.isArray(raw) || raw.length === 0) return
  const beats = parseStreamGeneratedBeats(raw)
  if (!beats.length) return
  persistAssistBeatSession(props.slug, ch, beats)
  if (currentChapter.value?.number === ch) {
    assistStreamBeatSession.value = { chapterNumber: ch, beats: [...beats] }
  }
}

function handleAutopilotBeatsPlanned(payload: {
  chapterNumber: number
  beats: Array<Record<string, unknown>>
}) {
  const ch = payload.chapterNumber
  if (!Number.isFinite(ch) || ch < 1) return
  const beats = parseStreamGeneratedBeats(payload.beats)
  if (!beats.length) return
  persistAssistBeatSession(props.slug, ch, beats)
  if (currentChapter.value?.number === ch) {
    assistStreamBeatSession.value = { chapterNumber: ch, beats: [...beats] }
    beatTabBump.value += 1
  }
}

const railAssistBeatSession = computed(() => {
  const manual = assistStreamBeatSession.value
  if (manual?.beats.length) return manual
  return autopilotPlannedBeatSession.value
})

async function persistMicroBeatsToDb(chapterNumber: number, beats: StreamGeneratedBeat[]) {
  if (chapterNumber < 1 || !beats.length) return
  try {
    await chapterApi.upsertChapterMicroBeats(props.slug, chapterNumber, toChapterMicroBeatPayloads(beats))
    desk.nudgeRailAfterGeneration()
  } catch {
    /* 内存 / sessionStorage 节拍仍可供侧栏展示 */
  }
}

function applyAssistStreamBeats(chapterNumber: number, beats: StreamGeneratedBeat[]) {
  if (chapterNumber < 1 || !beats.length) return
  assistStreamBeatSession.value = { chapterNumber, beats: [...beats] }
  persistAssistBeatSession(props.slug, chapterNumber, beats)
  void persistMicroBeatsToDb(chapterNumber, beats)
}

function restoreAssistStreamBeatsForChapter(chapterNumber: number | null | undefined) {
  if (chapterNumber == null || chapterNumber < 1) {
    assistStreamBeatSession.value = null
    return
  }
  const sess = assistStreamBeatSession.value
  if (sess?.chapterNumber === chapterNumber && sess.beats.length > 0) return
  const loaded = loadAssistBeatSession(props.slug, chapterNumber)
  if (loaded?.length) {
    assistStreamBeatSession.value = { chapterNumber, beats: loaded }
  } else if (sess?.chapterNumber !== chapterNumber) {
    assistStreamBeatSession.value = null
  }
}

const MAX_SSE_LOG_LINES = GENERATE_STREAM_LOG_LIMIT
const generateSseLog = ref<GenerateStreamLogLine[]>([])
/** 与 SSE phase 对齐，用于章前规划骨架显隐 */
const generateStreamPhase = ref('')
const outlinePartitionChunkCount = ref(0)
const proseChunkLogCount = ref(0)
const sseLogScrollEl = ref<HTMLElement | null>(null)

function pushGenerateSseLog(tag: string, msg: string) {
  generateSseLog.value = appendGenerateStreamLog(generateSseLog.value, { tag, msg })
  void nextTick(() => scrollGenerateSseLogBottom(false))
}

function scrollGenerateSseLogBottom(smooth = true) {
  const el = sseLogScrollEl.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
}

function sseTagType(tag: string) {
  return generateStreamTagType(tag)
}

const planningSkeletonRows = computed(() => {
  if (!generateInProgress.value || generateStreamPhase.value !== 'outline_planning') return 0
  const c = outlinePartitionChunkCount.value
  // 首条骨架在 phase 到时即显示；每收到一段 outline_partition 增量多一行（上限 8）
  return Math.min(8, Math.max(1, c + 1))
})

/** 重新生成模式：开启时弹窗中显示「改进方向」输入框，并在生成前自动快照当前内容 */
const isRegenerationMode = ref(false)
/** 重新生成改进方向（可选，传给后端 regeneration_guidance） */
const regenerationGuidance = ref('')
/** 是否正在保存草稿（重新生成前的快照） */
const savingDraftBeforeRegen = ref(false)

// Autopilot 状态
const autopilotStatus = ref<AutopilotStatus | null>(null)
const isAutopilotRunning = computed(() => autopilotStatus.value?.autopilot_status === 'running')
const isAutopilotWriting = computed(() =>
  isAutopilotRunning.value && autopilotStatus.value?.current_stage === 'writing'
)
const writingPipelineStep = computed(() => {
  if (!isAutopilotRunning.value) return null
  const ix = Number(autopilotStatus.value?.story_pipeline_wave_index)
  return Number.isFinite(ix) && ix >= 1 ? ix : null
})
/** 守护进程章末审阅快照（与 /autopilot/status 同源） */
const autopilotChapterReview = computed(() => autopilotStatus.value?.last_chapter_audit ?? null)

/** 在辅助撰稿且全托管运行中：只读，不可改稿与生成 */
const isAssistedReadOnly = computed(
  () => workMode.value === 'assisted' && isAutopilotRunning.value
)

/** 与左侧章节「已收稿」、结构树同步：全托管推进时刷新 desk（首次快照只记录不 emit，避免与进入页重复请求） */
const lastAutopilotDeskSnap = ref<string | null>(null)

/** 尾部去抖：SSE + 轮询短时间连发时合并为一次整桌刷新；避免「跳过」导致永不 emit 的旧逻辑 */
const deskRefreshEmit = useDebouncedTask(
  () => {
    emit('chapterUpdated')
  },
  () => runtimePerformance.workbench.deskRefreshEmitDebounceMs,
)

function emitDeskRefreshDebounced() {
  deskRefreshEmit.schedule()
}

function maybeEmitDeskRefresh(status: Record<string, unknown> | null | undefined) {
  const next = buildAutopilotDeskSnapshot(status)
  if (next === '') return
  if (lastAutopilotDeskSnap.value === null) {
    lastAutopilotDeskSnap.value = next
    return
  }
  if (lastAutopilotDeskSnap.value === next) return
  lastAutopilotDeskSnap.value = next
  emitDeskRefreshDebounced()
}

const handleAutopilotStatusChange = (status: any) => {
  applyAutopilotStatusPayload(status)
}

/** 排除纳秒级抖动字段（context_tokens、daemon 心跳等），仅在「读者可见状态」变化时更新 Vue，减轻辅助撰稿区重绘 */
const lastAutopilotReactiveFp = ref<string>('')

function applyAutopilotStatusPayload(status: Record<string, unknown> | null | undefined) {
  if (status == null) {
    autopilotStatus.value = null
    lastAutopilotReactiveFp.value = ''
    maybeEmitDeskRefresh(status)
    return
  }
  const fp = buildAutopilotReactiveFingerprint(status)
  if (fp !== lastAutopilotReactiveFp.value) {
    lastAutopilotReactiveFp.value = fp
    autopilotStatus.value = status
    syncPlannedBeatsFromAutopilotStatus(status)
  }
  maybeEmitDeskRefresh(status)
}

/** SSE 已广播刷新信号时去抖触发 desk 刷新（与 maybeEmitDeskRefresh 共用去抖逻辑） */
function handleAutopilotDeskRefreshFromStream() {
  emitDeskRefreshDebounced()
}

let guardrailSnapshotRefreshTimer: number | null = null

function clearGuardrailSnapshotRefreshTimer() {
  if (guardrailSnapshotRefreshTimer != null) {
    window.clearTimeout(guardrailSnapshotRefreshTimer)
    guardrailSnapshotRefreshTimer = null
  }
}

function scheduleGuardrailSnapshotRefresh() {
  clearGuardrailSnapshotRefreshTimer()
  guardrailSnapshotRefreshTimer = window.setTimeout(
    () => {
      guardrailSnapshotRefreshTimer = null
      void loadGuardrailSnapshot({ force: true })
    },
    runtimePerformance.workbench.guardrailSnapshotRefreshDelayMs,
  )
}

onBeforeUnmount(() => {
  clearGuardrailSnapshotRefreshTimer()
  if (editorDraft.value) rememberChapterDraft(editorDraft.value)
  void flushPendingEditor()
  editorRequestGeneration += 1
  rememberGeneratedContent()
  void stopGenerate(false)
})

useAssistedAutopilotStatus({
  slug: computed(() => props.slug),
  enabled: computed(() => workMode.value === 'assisted'),
  onStatus: applyAutopilotStatusPayload,
})

/** 自动驾驶章节内容流更新：实时显示正在写作的内容 */
const streamingChapterNumber = ref<number | null>(null)
const streamingContent = ref('')
const streamingBeatIndex = ref(0)
const beatTabBump = ref(0)

function handleChapterContentUpdate(data: { chapterNumber: number; content: string; wordCount: number }) {
  streamingChapterNumber.value = data.chapterNumber
  streamingContent.value = data.content

  // 如果当前正在查看的章节就是正在写作的章节，实时更新编辑框内容
  if (currentChapter.value && currentChapter.value.number === data.chapterNumber) {
    chapterContent.value = data.content
  }
}

/** SSE 增量 chunk：驱动编辑区与托管预览同步打字机式更新（整章快照事件较少，仅靠 snapshot 会卡顿） */
function handleChapterChunkStream(data: {
  chunk: string
  beatIndex: number
  content: string
  chapterNumber: number
  isSnapshot?: boolean
}) {
  const n = data.chapterNumber
  if (!n) return
  streamingChapterNumber.value = n
  streamingContent.value = data.content
  streamingBeatIndex.value = data.beatIndex ?? 0
  if (currentChapter.value && currentChapter.value.number === n) {
    chapterContent.value = data.content
  }
}

watch(
  () => props.currentChapterId,
  (id) => {
    const ch = id != null ? props.chapters.find(c => c.id === id)?.number : null
    restoreAssistStreamBeatsForChapter(ch ?? null)
  },
  { immediate: true },
)

watch(
  () => props.slug,
  () => {
    rememberGeneratedContent()
    const previousRequest = generationRequest.value
    if (previousRequest) void cancelChapterGeneration(previousRequest.slug, previousRequest.id).catch(() => undefined)
    generationRequest.value = null
    resumeGenerationOperation = null
    generatedDraftGeneration += 1
    generateAbortCtrl.value?.abort()
    generateAbortCtrl.value = null
    generatedDraft.value = null
    generateInProgress.value = false
    generatingChapterId.value = null
    generatedContent.value = ''
    lastAutopilotDeskSnap.value = null
    lastAutopilotReactiveFp.value = ''
    assistStreamBeatSession.value = null
    assistStreamFailedChapter.value = null
    assistStreamPlanFailedChapter.value = null
    generateSseLog.value = []
    generateStreamPhase.value = ''
    outlinePartitionChunkCount.value = 0
    proseChunkLogCount.value = 0
  }
)

/** 左侧切换章节（或路由）导致章 id 变化时回到辅助撰稿 */
watch(
  () => props.currentChapterId,
  (id, prev) => {
    if (id != null && id !== prev) {
      workMode.value = 'assisted'
    }
  }
)

// 章节编辑
const chapterContent = ref('')
const originalContent = ref('')
const editorDraft = shallowRef<ChapterDraftSession | null>(null)
const generatedDraft = shallowRef<ChapterDraftSession | null>(null)
const generatedStatus = ref<GenerationTerminalStatus | 'idle' | 'running' | 'disconnected' | 'cancel_unknown'>('idle')
const generationRequest = shallowRef<{ slug: string; id: string } | null>(null)
const generateResumeCursor = ref(0)
let resumeGenerationOperation: (() => Promise<void>) | null = null
let editorRequestGeneration = 0
let generatedDraftGeneration = 0
const loading = computed(() => props.chapterLoading)
const saving = ref(false)

// Scene Director 开关
const useSceneDirector = ref(false)
const analyzingScene = ref(false)
const sceneDirectorError = ref('')

// 张力诊断
const showTensionModal = ref(false)
const tensionLoading = ref(false)
const tensionStuckReason = ref('')
const tensionResult = ref<TensionDiagnosis | null>(null)

const openTensionModal = () => {
  tensionResult.value = null
  tensionStuckReason.value = ''
  showTensionModal.value = true
}

const runTensionSlingshot = async () => {
  if (!currentChapter.value) return
  if (isAssistedReadOnly.value) {
    message.warning('托管运行中不可使用张力诊断')
    return
  }
  tensionLoading.value = true
  try {
    tensionResult.value = await tensionApi.slingshot(props.slug, {
      novel_id: props.slug,
      chapter_number: currentChapter.value.number,
      stuck_reason: tensionStuckReason.value || undefined,
    })
  } catch {
    message.error('分析失败，请稍后重试')
  } finally {
    tensionLoading.value = false
  }
}

// 上下文预览
const contextPreview = ref<ContextPreviewResult | null>(null)
const loadingContext = ref(false)

const chapterSelectOptions = computed(() =>
  props.chapters.map((ch) => ({
    label: `${ordinalUnit(ch.number)}${ch.title ? ` · ${ch.title.slice(0, 22)}` : ''}`,
    value: ch.id,
  }))
)

// ── AI Panel helpers ──
const llmProfileOptions = computed(() =>
  llmProfiles.value.map(p => ({
    label: `${p.name} (${p.model || p.protocol})`,
    value: p.id,
  }))
)

async function loadLLMProfilesForModal() {
  if (llmProfiles.value.length > 0) return
  llmProfilesLoading.value = true
  try {
    const data = await llmControlApi.getPanel()
    llmProfiles.value = data.config.profiles || []
    if (!generateProfileId.value && data.config.active_profile_id) {
      generateProfileId.value = data.config.active_profile_id
    }
  } catch {
    /* silently fail; profile selector will be empty */
  } finally {
    llmProfilesLoading.value = false
  }
}

function buildPromptVariables(): Record<string, string> | null {
  const vars: Record<string, string> = {}
  for (const pair of scriptPromptVarPairs.value) {
    if (pair.key) vars[pair.key] = pair.value
  }
  for (const pair of prosePromptVarPairs.value) {
    if (pair.key) vars[pair.key] = pair.value
  }
  return Object.keys(vars).length > 0 ? vars : null
}

watch(showGenerateModal, (visible) => {
  if (!visible) {
    useCustomScriptPrompt.value = false
    useCustomProsePrompt.value = false
  }
})

const modalTargetChapter = computed(() => {
  const id = generateTargetChapterId.value
  if (id == null) return null
  return props.chapters.find(ch => ch.id === id) ?? null
})

const previewContext = async () => {
  const chNum = modalTargetChapter.value?.number
  if (!chNum) return
  loadingContext.value = true
  try {
    contextPreview.value = await retrieveContext(
      props.slug,
      chNum,
      generateOutline.value || `${ordinalUnit(chNum)}：承接前情，推进主线`,
    )
  } catch {
    contextPreview.value = null
  } finally {
    loadingContext.value = false
  }
}

async function onOutlineBlurAnalyze() {
  const ch = modalTargetChapter.value
  const outline = generateOutline.value.trim()
  if (!ch || !outline || outlineBlurAnalyzing.value || generateInProgress.value) {
    return
  }
  outlineBlurAnalyzing.value = true
  try {
    const analysis = await analyzeScene(props.slug, ch.number, outline)
    blurSceneCache.value = analysis as Record<string, unknown>
  } catch {
    blurSceneCache.value = undefined
  } finally {
    outlineBlurAnalyzing.value = false
  }
}

function clearWorkflowQc() {
  lastWorkflowResult.value = null
  lastQcChapterNumber.value = null
}

function clearGeneratedDraft() {
  detachGeneratedRequest()
  if (generatedDraft.value) {
    generatedDraft.value.content = generatedDraft.value.originalContent
    rememberChapterDraft(generatedDraft.value, 'generated')
  }
  generatedContent.value = ''
  generatedDraft.value = null
  generatedStatus.value = 'idle'
  clearWorkflowQc()
}

function detachGeneratedRequest() {
  const previousRequest = generationRequest.value
  generatedDraftGeneration += 1
  resumeGenerationOperation = null
  generateAbortCtrl.value?.abort()
  generateAbortCtrl.value = null
  generationRequest.value = null
  if (previousRequest) void cancelChapterGeneration(previousRequest.slug, previousRequest.id).catch(() => undefined)
}

watch(generateTargetChapterId, () => {
  blurSceneCache.value = undefined
  contextPreview.value = null
  if (!generateInProgress.value) {
    rememberGeneratedContent()
    detachGeneratedRequest()
    generatedDraft.value = null
    generatedContent.value = ''
    generatedStatus.value = 'idle'
    const target = modalTargetChapter.value
    if (target) {
      const recovery = new ChapterDraftSession(props.slug, target.number, '', { revision: 0, content_hash: '', etag: '' })
      if (restoreChapterDraft(recovery, 'generated')) {
        generatedDraft.value = recovery
        generatedContent.value = recovery.content
        generatedStatus.value = 'interrupted'
        message.info('已恢复尚未保存的生成稿；保存时会检查原章节版本')
      }
    }
  }
})

function rememberGeneratedContent() {
  if (generatedDraft.value && generatedContent.value) {
    generatedDraft.value.content = generatedContent.value
    rememberChapterDraft(generatedDraft.value, 'generated')
  }
}

async function resumeLastGeneration() {
  if (generateInProgress.value || generatedStatus.value !== 'disconnected' || !resumeGenerationOperation) return
  await resumeGenerationOperation()
}

// AbortController：点「停止」时真正取消后端 SSE 流
const generateAbortCtrl = ref<AbortController | null>(null)

// 正在生成的章节 ID（null = 不在生成中）
// 与 currentChapterId 解耦：用户可以切换章节，生成仍在后台继续
const generatingChapterId = ref<number | null>(null)

/** 当前视图是否正处于生成中（快速生成按钮 loading） */
const generating = computed(
  () =>
    generateInProgress.value &&
    generatingChapterId.value !== null &&
    generatingChapterId.value === props.currentChapterId
)

const currentChapter = computed(() => {
  if (!props.currentChapterId) return null
  return props.chapters.find(ch => ch.id === props.currentChapterId) || null
})

const deskChapterTitle = computed(() => {
  const ch = currentChapter.value
  if (!ch) return ''
  return ordinalUnit(ch.number)
})

const nextProseChapterNumber = computed(() => {
  return getNextProseChapterNumber(props.chapters)
})

/** 当前是否有可重写的正文：以编辑器 `chapterContent` 为准（列表项通常不带全文，不能用 currentChapter.content） */
const hasChapterContent = computed(() => {
  return hasEditableChapterContent(chapterContent.value, currentChapter.value?.content)
})

const prosePrimaryGenerationTarget = computed<ProseGenerationChapterTarget | null>(() => {
  return selectProsePrimaryGenerationTarget({
    proseOnlyWorkbench,
    currentChapter: currentChapter.value,
    chapters: props.chapters,
    hasChapterContent: hasChapterContent.value,
    nextChapterNumber: nextProseChapterNumber.value,
  })
})

const prosePrimaryActionLabel = computed(() => {
  return getProsePrimaryActionLabel(proseOnlyWorkbench, hasChapterContent.value)
})

const signalStrip = computed(() => {
  const r = autopilotChapterReview.value
  const ch = currentChapter.value
  if (!r || !ch || r.chapter_number !== ch.number) return null
  return {
    tension: r.tension ?? 0,
    sync: !!r.narrative_sync_ok,
  }
})

const currentChapterNumber = computed(() => currentChapter.value?.number ?? null)
const { snapshot: guardrailSnapshot, load: loadGuardrailSnapshot } = useChapterGuardrailSnapshot({
  slug: computed(() => props.slug),
  chapterNumber: currentChapterNumber,
  refreshKey: computed(() => deskTick.value),
})

const hasChanges = computed(() => {
  return chapterContent.value !== originalContent.value || !!editorDraft.value?.dirty
})

const editorTitle = computed({
  get: () => editorDraft.value?.title ?? currentChapter.value?.title ?? '',
  set: (value: string) => {
    if (!editorDraft.value) return
    editorDraft.value.title = value
    triggerRef(editorDraft)
    rememberChapterDraft(editorDraft.value)
  },
})

const wordCount = computed(() => {
  // 🔥 流式写作时取流式内容长度，否则取编辑框内容长度
  if (isAutopilotRunning.value && streamingChapterNumber.value === currentChapter.value?.number && streamingContent.value) {
    return streamingContent.value.length
  }
  return chapterContent.value.length
})

/** 托管流式：用 /status 的已定稿字数与单章目标拆分展示，避免只显示「三千多字」误解为终稿 */
const streamingWordCountHint = computed((): string | null => {
  if (!isAutopilotRunning.value || streamingChapterNumber.value !== currentChapter.value?.number || !streamingContent.value) {
    return null
  }
  const st = autopilotStatus.value
  const tgt = Math.max(
    0,
    Number(st?.chapter_target_words ?? st?.target_words_per_chapter ?? 0)
  )
  if (!tgt) return null
  const acc = Math.max(0, Number(st?.accumulated_words ?? 0))
  const live = streamingContent.value.length
  const over = Math.max(0, live - acc)
  if (over > 0) {
    return `已定 ${acc}/${tgt} · 流式 +${over}`
  }
  return `已定 ${live}/${tgt}`
})

/** 🔥 编辑框显示内容：流式时显示流式内容，否则显示普通内容 */
const editorDisplayContent = computed({
  get: () => {
    if (isAutopilotRunning.value && streamingChapterNumber.value === currentChapter.value?.number && streamingContent.value) {
      return streamingContent.value
    }
    return chapterContent.value
  },
  set: (val: string) => {
    chapterContent.value = val
    if (editorDraft.value) {
      editorDraft.value.content = val
      rememberChapterDraft(editorDraft.value)
    }
  }
})

// A list refresh cannot rebase unsaved text.  Only a full chapter GET carries
// the content and version that belong together; late/older snapshots are ignored.
watch(
  () => [props.slug, props.currentChapterId, props.chapterContent, props.chapterVersion] as const,
  ([novelId, id, newContent, version]) => {
    const previous = editorDraft.value
    const sameDocument = previous?.slug === novelId && previous?.chapterNumber === id
    if (sameDocument && (previous.dirty || previous.saving)) return
    if (sameDocument && version && version.revision < previous.version.revision) return
    if (previous && !sameDocument) {
      rememberChapterDraft(previous)
      if (previous.dirty) void saveEditorSession(previous, false)
    }
    editorRequestGeneration += 1
    const row = props.chapters.find(ch => ch.id === id)
    const snapshotVersion = version ?? row
    if (id == null || !snapshotVersion) {
      editorDraft.value = null
      chapterContent.value = ''
      originalContent.value = ''
      return
    }
    const session = new ChapterDraftSession(novelId, id, newContent, { ...snapshotVersion, title: row?.title })
    const restored = restoreChapterDraft(session)
    editorDraft.value = session
    chapterContent.value = session.content
    originalContent.value = session.originalContent
    if (restored) message.warning('已恢复本地未保存正文；保存时仍会检查原版本')
  },
  { immediate: true },
)

// 切换回正在生成的章节时，自动打开生成弹窗（让用户看到进度）
watch(() => props.currentChapterId, (id) => {
  if (!proseOnlyWorkbench && id !== null && id === generatingChapterId.value) {
    showGenerateModal.value = true
  }
})

const handleContentChange = () => {
  if (editorDraft.value) {
    editorDraft.value.content = chapterContent.value
    rememberChapterDraft(editorDraft.value)
  }
}

function showRevisionConflict(error: unknown, session: ChapterDraftSession, isGenerated = false): boolean {
  const conflict = getChapterRevisionConflict(error)
  if (!conflict) return false
  dialog.warning({
    title: '章节版本冲突：本地正文已保留',
    content: () => h('div', [
      h('p', `服务器已更新到版本 ${conflict.current.revision}。请先比较差异；重新加载会更新章节编辑器${isGenerated ? '，生成稿仍保留在弹窗中' : '并替换当前正文'}。`),
      h('pre', { style: 'white-space:pre-wrap;max-height:50vh;overflow:auto;font-size:12px' }, conflict.diff || '正文相同，但版本已经变化。'),
    ]),
    positiveText: '重新加载服务器版本',
    negativeText: '保留本地编辑',
    onPositiveClick: () => {
      if (editorDraft.value?.slug !== session.slug || editorDraft.value.chapterNumber !== session.chapterNumber) return
      const fresh = new ChapterDraftSession(session.slug, session.chapterNumber, conflict.current.content || '', conflict.current)
      editorDraft.value = fresh
      chapterContent.value = fresh.content
      originalContent.value = fresh.content
      rememberChapterDraft(fresh)
      editorRequestGeneration += 1
    },
  })
  return true
}

async function saveEditorSession(session: ChapterDraftSession, notify = true): Promise<boolean> {
  try {
    const saved = await session.save(chapterApi.updateChapter)
    rememberChapterDraft(session)
    if (editorDraft.value === session) {
      chapterContent.value = session.content
      originalContent.value = session.originalContent
      triggerRef(editorDraft)
    }
    if (saved && props.slug === session.slug) {
      if (notify) message.success(saved.derived_status === 'deferred' ? '正文已保存，章后分析已排队' : '保存成功')
      emit('chapterUpdated')
      if (editorDraft.value === session) scheduleGuardrailSnapshotRefresh()
    }
    return true
  } catch (error) {
    rememberChapterDraft(session)
    if (!showRevisionConflict(error, session)) message.error(formatApiError(error, '保存失败，本地正文已保留'))
    return false
  }
}

const handleSave = async (): Promise<boolean> => {
  const session = editorDraft.value
  if (!session) return true
  if (isAssistedReadOnly.value) {
    message.warning('托管运行中不可保存，请先停止托管或仅阅读正文')
    return false
  }
  session.content = chapterContent.value
  saving.value = true
  try {
    return await saveEditorSession(session)
  } finally {
    saving.value = false
  }
}

async function flushPendingEditor(): Promise<boolean> {
  const session = editorDraft.value
  if (!session) return true
  while (session.dirty || session.saving) {
    if (!await saveEditorSession(session, false)) return false
  }
  return true
}

function handleHistoryRestored(chapter: ChapterDTO) {
  const session = editorDraft.value
  if (!session || session.slug !== chapter.novel_id || session.chapterNumber !== chapter.number) return
  editorDraft.value = new ChapterDraftSession(session.slug, session.chapterNumber, chapter.content, chapter)
  chapterContent.value = chapter.content
  originalContent.value = chapter.content
  rememberChapterDraft(editorDraft.value)
  editorRequestGeneration += 1
  emit('chapterUpdated')
  scheduleGuardrailSnapshotRefresh()
}

const handleReload = async () => {
  const session = editorDraft.value
  if (!session) return
  if (!await flushPendingEditor()) return
  const generation = ++editorRequestGeneration
  try {
    const fresh = await chapterApi.getChapter(session.slug, session.chapterNumber)
    if (generation !== editorRequestGeneration || editorDraft.value !== session || session.dirty || session.saving) return
    editorDraft.value = new ChapterDraftSession(session.slug, session.chapterNumber, fresh.content ?? '', fresh)
    chapterContent.value = fresh.content ?? ''
    originalContent.value = fresh.content ?? ''
    rememberChapterDraft(editorDraft.value)
    message.success('已重新加载')
  } catch (error) {
    if (generation === editorRequestGeneration) message.error(formatApiError(error, '加载失败，请稍后重试'))
  }
}

async function openProseInvocationForChapter(
  target: ProseGenerationChapterTarget,
  options?: {
    userRequirements?: string
  },
) {
  if (generateInProgress.value) return
  const chapterNumber = target.number
  generatingChapterId.value = target.id
  generateInProgress.value = true
  try {
    const payload = await aiInvocationApi.create({
      operation: 'chapter.generate.prose',
      node_key: 'chapter-prose-generation',
      policy: featureFlags.aiInvocationDebug ? 'FULL_INTERACTIVE' : 'DIRECT',
      context: {
        novel_id: props.slug,
        chapter_number: chapterNumber,
      },
      variables: {
        novel_title: props.bookTitle || props.slug,
        chapter_number: chapterNumber,
        chapter_title: target.title || '',
        user_requirements: options?.userRequirements || '',
      },
    })
    if (props.chapters.some(ch => ch.number === chapterNumber)) {
      emit('selectChapter', chapterNumber, target.title || '')
    }
    aiInvocationStore.openFromResponse(payload)
    if (payload.session?.status === 'completed') {
      emit('selectChapter', chapterNumber, target.title || '')
      emit('chapterUpdated')
      if (currentChapter.value?.number === chapterNumber) {
        void handleReload()
      }
      return
    }
    if (payload.session?.id) {
      const stopListening = aiInvocationStore.onSessionUpdate(payload.session.id, (nextPayload) => {
        if (nextPayload.session?.status !== 'completed') return
        stopListening()
        emit('selectChapter', chapterNumber, target.title || '')
        emit('chapterUpdated')
        if (currentChapter.value?.number === chapterNumber) {
          void handleReload()
        }
      })
    }
  } catch (err) {
    message.error(`创建正文生成任务失败：${formatApiError(err, '未知错误')}`)
  } finally {
    generateInProgress.value = false
    generatingChapterId.value = null
  }
}

const handleGenerateChapter = async () => {
  if (proseOnlyWorkbench) {
    const target = prosePrimaryGenerationTarget.value
    if (!target) return
    await openProseInvocationForChapter(target)
    return
  }
  if (!currentChapter.value) return
  if (isAssistedReadOnly.value) {
    message.warning('托管运行中不可使用快速生成')
    return
  }

  isRegenerationMode.value = false
  regenerationGuidance.value = ''
  generateTargetChapterId.value = currentChapter.value.id
  generateOutline.value = `${ordinalUnit(currentChapter.value.number)}：${currentChapter.value.title || ''}

承接前情，推进主线与人物节拍；保持人设与叙事节奏一致。`
  generatedContent.value = ''
  contextPreview.value = null
  blurSceneCache.value = undefined
  showGenerateModal.value = true
  void loadLLMProfilesForModal()
}

const handleRegenerateChapter = async () => {
  if (!currentChapter.value) return
  if (isAssistedReadOnly.value) {
    message.warning('托管运行中不可使用重新生成')
    return
  }

  if (proseOnlyWorkbench) {
    try {
      await saveChapterDraft(props.slug, currentChapter.value.number, 'pre_regen')
    } catch (e: unknown) {
      const status = getHttpStatus(e)
      const detail = formatApiError(e, '未知错误')
      if (status === 422 || detail.includes('内容为空')) {
        message.warning('当前无正文可快照，将直接进入重新生成面板')
      } else {
        message.warning(`历史草稿快照失败，将继续打开面板：${detail}`)
      }
    }
    await openProseInvocationForChapter(currentChapter.value, {
      userRequirements: '本次为重新生成当前章节。请保留核心设定与章节定位，但整体重写为全新正文，不要沿袭现有措辞。',
    })
    return
  }

  isRegenerationMode.value = true
  regenerationGuidance.value = ''
  generateTargetChapterId.value = currentChapter.value.id
  // 列表项不带 outline，统一用默认模板做种子；用户可在弹窗里编辑
  generateOutline.value = `${ordinalUnit(currentChapter.value.number)}：${currentChapter.value.title || ''}

承接前情，推进主线与人物节拍；保持人设与叙事节奏一致。`
  generatedContent.value = ''
  contextPreview.value = null
  blurSceneCache.value = undefined
  showGenerateModal.value = true
  void loadLLMProfilesForModal()
}

const handleStartGenerate = async () => {
  const target = modalTargetChapter.value
  if (!target) {
    message.warning('请选择目标章节')
    return
  }
  if (isAssistedReadOnly.value) {
    message.warning('托管运行中不可手动生成')
    return
  }

  const targetChapterId = target.id
  const targetChapterNumber = target.number
  const novelId = props.slug
  const generation = ++generatedDraftGeneration
  const isCurrentGeneration = () => generation === generatedDraftGeneration && novelId === props.slug
  generatingChapterId.value = targetChapterId
  generateInProgress.value = true
  generatedDraft.value = null
  generationRequest.value = null
  resumeGenerationOperation = null
  generateResumeCursor.value = 0
  generatedStatus.value = 'running'
  try {
    if (editorDraft.value?.chapterNumber === targetChapterNumber && !await flushPendingEditor()) {
      generateInProgress.value = false
      generatingChapterId.value = null
      return
    }
    const baseline = await chapterApi.getChapter(novelId, targetChapterNumber)
    if (!isCurrentGeneration()) return
    generatedDraft.value = new ChapterDraftSession(novelId, targetChapterNumber, baseline.content || '', baseline)
  } catch (error) {
    if (isCurrentGeneration()) {
      message.error(formatApiError(error, '无法读取目标章节版本，生成未开始'))
      generateInProgress.value = false
      generatingChapterId.value = null
    }
    return
  }
  assistStreamBeatSession.value = null
  assistStreamFailedChapter.value = null
  assistStreamPlanFailedChapter.value = null
  generateSseLog.value = []
  generateStreamPhase.value = ''
  outlinePartitionChunkCount.value = 0
  proseChunkLogCount.value = 0
  generatedContent.value = ''
  sceneDirectorError.value = ''
  lastWorkflowResult.value = null
  lastQcChapterNumber.value = null
  streamPhaseLabel.value = '连接中…'
  streamProgressPct.value = 8
  streamStats.value = { chars: 0, estimated_tokens: 0, chunks: 0 }
  pushGenerateSseLog('SSE', '正在连接 generate-chapter-stream…')

  let ctrl = new AbortController()
  generateAbortCtrl.value = ctrl

  let sceneDirectorResult: Record<string, unknown> | undefined = blurSceneCache.value
  if (useSceneDirector.value && !sceneDirectorResult) {
    analyzingScene.value = true
    try {
      const outline = generateOutline.value || `${ordinalUnit(targetChapterNumber)}：承接前情，推进主线`
      const analysis = await analyzeScene(novelId, targetChapterNumber, outline)
      sceneDirectorResult = analysis as Record<string, unknown>
    } catch (e: unknown) {
      if (isCurrentGeneration()) sceneDirectorError.value = e instanceof Error ? e.message : '分析失败'
    } finally {
      if (isCurrentGeneration()) analyzingScene.value = false
    }
  }

  if (!isCurrentGeneration()) return

  const defaultOutline = `${ordinalUnit(targetChapterNumber)}：承接前情，推进主线`

  // 重新生成模式：先快照当前内容；快照失败时弹确认（422 无正文仅提示后继续）
  if (isRegenerationMode.value) {
    savingDraftBeforeRegen.value = true
    try {
      await saveChapterDraft(novelId, targetChapterNumber, 'pre_regen')
    } catch (e: unknown) {
      const status = getHttpStatus(e)
      const detail = formatApiError(e, '未知错误')
      if (status === 422 || detail.includes('内容为空')) {
        message.warning('当前无正文可快照，将直接继续生成')
      } else {
        const proceed = await new Promise<boolean>((resolve) => {
          dialog.warning({
            title: '未能保存历史草稿',
            content: `无法将当前版本快照到历史（${detail}）。若继续重新生成，原内容可能无法从草稿恢复。是否仍要继续？`,
            positiveText: '继续生成',
            negativeText: '取消',
            maskClosable: false,
            onPositiveClick: () => {
              resolve(true)
            },
            onNegativeClick: () => {
              resolve(false)
            },
            onClose: () => {
              resolve(false)
            },
          })
        })
        if (!proceed) {
          generateInProgress.value = false
          generatingChapterId.value = null
          generateAbortCtrl.value = null
          streamPhaseLabel.value = ''
          streamProgressPct.value = 0
          return
        }
      }
    } finally {
      savingDraftBeforeRegen.value = false
    }
  }

  if (!isCurrentGeneration()) return

  const requestId = crypto.randomUUID().replaceAll('-', '')
  const requestData: GenerateChapterWithContextPayload = {
        chapter_number: targetChapterNumber,
        outline: generateOutline.value || defaultOutline,
        scene_director_result: sceneDirectorResult,
        regeneration_guidance: isRegenerationMode.value && regenerationGuidance.value.trim()
          ? regenerationGuidance.value.trim()
          : undefined,
        profile_id: generateProfileId.value || undefined,
        script_prompt_template: useCustomScriptPrompt.value
          ? customScriptTemplate.value || undefined
          : undefined,
        prose_prompt_template: useCustomProsePrompt.value
          ? customProseTemplate.value || undefined
          : undefined,
        prompt_variables: buildPromptVariables() || undefined,
      }
  const consumeCurrentStream = async () => {
    try {
      await consumeGenerateChapterStream(
        novelId,
        { ...requestData, request_id: requestId, resume_after: generateResumeCursor.value },
        {
          signal: ctrl.signal,
          onRequest: (requestId) => {
            if (isCurrentGeneration()) generationRequest.value = { slug: novelId, id: requestId }
          },
          onCursor: (sequence) => {
            if (isCurrentGeneration()) generateResumeCursor.value = sequence
          },
          onReconnect: (attempt) => {
            if (!isCurrentGeneration()) return
            streamPhaseLabel.value = '正在恢复连接…'
            pushGenerateSseLog('连接', `恢复同一生成请求 · 第 ${attempt} 次`)
          },
          onApprovalRequired: (sessionId) => {
            if (isCurrentGeneration()) void aiInvocationStore.open(sessionId).catch(() => message.error('审阅面板加载失败，请重试打开 AI 调用会话'))
          },
          onTerminal: (status) => {
            if (!isCurrentGeneration()) return
            generatedStatus.value = status
            streamPhaseLabel.value = { completed: '已完成', failed: '生成失败，片段已保留', cancelled: '已停止，片段已保留', interrupted: '服务中断，片段已保留', awaiting_review: '等待审阅' }[status]
            rememberGeneratedContent()
          },
          onPhase: (phase) => {
            if (!isCurrentGeneration()) return
            generateStreamPhase.value = phase
            streamPhaseLabel.value = streamPhaseToLabel(phase)
            streamProgressPct.value = streamPhaseToProgress(phase)
            pushGenerateSseLog('SSE', streamPhaseToLogLabel(phase))
          },
          onBeatsGenerated: (beats) => {
            if (!isCurrentGeneration()) return
            outlinePartitionChunkCount.value = 0
            generateStreamPhase.value = 'prose'
            streamPhaseLabel.value = streamPhaseToLabel('prose')
            streamProgressPct.value = streamPhaseToProgress('prose')
            pushGenerateSseLog(
              '规划',
              beats.length > 0 ? `历史拆拍结果 ×${beats.length}` : '规划未返回拆拍',
            )
            if (beats.length >= 2) {
              if (assistStreamPlanFailedChapter.value === targetChapterNumber) {
                assistStreamPlanFailedChapter.value = null
              }
            } else if (beats.length === 0) {
              assistStreamPlanFailedChapter.value = targetChapterNumber
            }
            applyAssistStreamBeats(targetChapterNumber, beats)
          },
          onLLMChunk: (stage, text) => {
            if (!isCurrentGeneration()) return
            if (stage === 'outline_partition') {
              outlinePartitionChunkCount.value += 1
              generateStreamPhase.value = 'outline_planning'
              streamPhaseLabel.value = '章节执行剧本准备…'
              streamProgressPct.value = Math.max(
                streamProgressPct.value,
                streamPhaseToProgress('outline_planning'),
              )
              const n = outlinePartitionChunkCount.value
              if (n === 1 || n % 4 === 0) {
                pushGenerateSseLog('规划', `执行剧本增量 ×${n}（+${text.length}）`)
              }
            }
          },
          onChunk: (text, stats) => {
            if (!isCurrentGeneration()) return
            generatedContent.value += text
            proseChunkLogCount.value += 1
            const pc = proseChunkLogCount.value
            if (pc === 1) {
              pushGenerateSseLog('正文', 'chunk 流式输出开始…')
            } else if (pc % 32 === 0) {
              pushGenerateSseLog('正文', `chunk ×${pc}`)
            }
            if (stats) {
              streamStats.value = stats
            }
          },
          onDone: (result) => {
            if (!isCurrentGeneration()) return
            pushGenerateSseLog('SSE', 'done · 生成完成')
            lastWorkflowResult.value = result
            lastQcChapterNumber.value = targetChapterNumber
            generatedContent.value = result.content
            generatedStatus.value = 'completed'
            rememberGeneratedContent()
            streamProgressPct.value = 100
            streamPhaseLabel.value = '已完成'
            assistStreamFailedChapter.value = null
            if (result.beats?.length) {
              applyAssistStreamBeats(targetChapterNumber, result.beats)
            }
            const beatCount =
              result.beats?.length ??
              (assistStreamBeatSession.value?.chapterNumber === targetChapterNumber
                ? assistStreamBeatSession.value.beats.length
                : 0)
            if (beatCount <= 1) {
              assistStreamPlanFailedChapter.value = targetChapterNumber
            } else if (assistStreamPlanFailedChapter.value === targetChapterNumber) {
              assistStreamPlanFailedChapter.value = null
            }
            if (props.currentChapterId === targetChapterId) {
              message.success('生成完成，质检已同步到本章侧栏')
            } else {
              message.success(`${ordinalUnit(targetChapterNumber)}生成完成，请在对应章侧栏查看质检`)
            }
            desk.nudgeRailAfterGeneration()
          },
          onError: (err) => {
            if (isCurrentGeneration() && !ctrl.signal.aborted) {
              generatedStatus.value = 'disconnected'
              rememberGeneratedContent()
              message.error(`生成失败: ${err}`)
              assistStreamFailedChapter.value = targetChapterNumber
              assistStreamPlanFailedChapter.value = targetChapterNumber
              pushGenerateSseLog('SSE', `error · ${err}`)
            }
          },
        }
      )
    } catch {
      if (isCurrentGeneration() && !ctrl.signal.aborted) {
        message.error('生成失败')
        assistStreamFailedChapter.value = targetChapterNumber
        assistStreamPlanFailedChapter.value = targetChapterNumber
        pushGenerateSseLog('SSE', 'catch · 请求异常')
      }
    } finally {
      if (!isCurrentGeneration()) return
      generateInProgress.value = false
      generatingChapterId.value = null
      generateAbortCtrl.value = null
      if (!ctrl.signal.aborted && generatedStatus.value === 'disconnected') {
        streamPhaseLabel.value = '连接中断，可恢复本次生成'
      }
    }
  }
  resumeGenerationOperation = async () => {
    if (!isCurrentGeneration() || isAssistedReadOnly.value) return
    ctrl = new AbortController()
    generateAbortCtrl.value = ctrl
    generateInProgress.value = true
    generatingChapterId.value = targetChapterId
    generatedStatus.value = 'running'
    streamPhaseLabel.value = '正在恢复本次生成…'
    await consumeCurrentStream()
  }
  await consumeCurrentStream()
}

const handleSaveGenerated = async () => {
  const saveTarget = modalTargetChapter.value
  const session = generatedDraft.value
  if (!saveTarget || !session || !generatedContent.value) return
  if (session.slug !== props.slug || session.chapterNumber !== saveTarget.number) {
    message.warning('生成稿与当前目标章节不一致，请重新选择生成时的章节')
    return
  }
  if (isAssistedReadOnly.value) {
    message.warning('托管运行中不可保存生成结果')
    return
  }
  const active = editorDraft.value
  if (active?.slug === session.slug && active.chapterNumber === session.chapterNumber && (active.dirty || active.saving)) {
    message.warning('当前章节还有未保存编辑，请先保存后再比较生成结果')
    return
  }
  const submitted = generatedContent.value
  session.content = submitted

  saving.value = true
  try {
    const sess = assistStreamBeatSession.value
    const mb =
      sess?.chapterNumber === saveTarget.number && sess.beats.length > 0
        ? toChapterMicroBeatPayloads(sess.beats)
        : undefined
    const saved = await session.save(chapterApi.updateChapter, {
      ...(mb?.length ? { micro_beats: mb } : {}),
    })
    rememberChapterDraft(session, 'generated')
    if (props.slug !== session.slug || generatedDraft.value !== session) return
    if (saved && editorDraft.value?.slug === session.slug && editorDraft.value.chapterNumber === session.chapterNumber && !editorDraft.value.dirty) {
      editorDraft.value = new ChapterDraftSession(session.slug, session.chapterNumber, saved.content, saved)
      chapterContent.value = saved.content
      originalContent.value = saved.content
      rememberChapterDraft(editorDraft.value)
    }
    message.success(`已保存到${ordinalUnit(saveTarget.number)}`)
    emit('chapterUpdated')
    if (generatedContent.value === submitted) showGenerateModal.value = false
    scheduleGuardrailSnapshotRefresh()
  } catch (error) {
    if (props.slug === session.slug && !showRevisionConflict(error, session, true)) {
      message.error(formatApiError(error, '保存失败，生成稿已保留'))
    }
  } finally {
    saving.value = false
  }
}

const stopGenerate = async (notify = true) => {
  const request = generationRequest.value
  rememberGeneratedContent()
  const cancellationGeneration = ++generatedDraftGeneration
  resumeGenerationOperation = null
  generateAbortCtrl.value?.abort()
  generateAbortCtrl.value = null
  generatingChapterId.value = null
  generateInProgress.value = false
  streamPhaseLabel.value = '正在停止，片段已保留'
  generateStreamPhase.value = ''
  outlinePartitionChunkCount.value = 0
  proseChunkLogCount.value = 0
  if (!request) {
    generatedStatus.value = 'cancelled'
    streamPhaseLabel.value = '已停止，片段已保留'
    if (notify) message.info('已停止生成准备，已有片段已保留')
    return
  }
  try {
    const response = await cancelChapterGeneration(request.slug, request.id)
    if (request.slug !== props.slug || generationRequest.value !== request || cancellationGeneration !== generatedDraftGeneration) return
    generatedStatus.value = response.status === 'running' ? 'cancel_unknown' : response.status
    if (response.status === 'running') {
      streamPhaseLabel.value = '停止尚未确认，可重试停止，片段已保留'
      if (notify) message.warning('后端仍未确认停止，请重试停止；已有片段已保留')
      return
    }
    streamPhaseLabel.value = response.status === 'cancelled' ? '已停止，片段已保留' : '请求已结束，片段已保留'
    if (notify) message.info(response.status === 'cancelled' ? '后端已停止生成，已有片段已保留' : '该生成请求已经结束，已有片段已保留')
  } catch {
    if (request.slug !== props.slug || generationRequest.value !== request || cancellationGeneration !== generatedDraftGeneration) return
    generatedStatus.value = 'cancel_unknown'
    streamPhaseLabel.value = '停止尚未确认，片段已保留'
    if (notify) message.warning('无法确认后端已停止，已有片段已保留')
  }
}

/** 左侧每次点选章节时由父组件调用，确保回到辅助撰稿（含重复点击当前章） */
function ensureAssistedMode() {
  workMode.value = 'assisted'
}

function openManagedWorkspace() {
  if (managedWorkbenchEnabled) workMode.value = 'managed'
}

defineExpose({ openManagedWorkspace, ensureAssistedMode, flushPendingEditor, streamingChapterNumber, writingPipelineStep })
useUpdateSaveBarrier(flushPendingEditor)
</script>

<style scoped>
.work-area {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--app-surface);
}

.work-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.work-mode-switch {
  flex-shrink: 0;
  display: flex;
  align-items: center;
}

/* 双语文案轨道略宽，避免挤字 */
.work-mode-switch :deep(.n-switch__rail) {
  min-width: 5.5rem;
}

.assisted-readonly-banner {
  flex-shrink: 0;
  margin: 0 16px 8px;
}

.assisted-stack {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chapter-desk-shell {
  flex: 1;
  min-height: 0;
  min-width: 0;
}

.desk-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.rail-column {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.rail-head {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--taleweft-split-border, rgba(0, 0, 0, 0.08));
}

.rail-scroll {
  flex: 1;
  min-height: 0;
}

.rail-scroll-pad {
  padding: 10px 10px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* rail 2-tab layout */
.rail-stream-bar {
  flex-shrink: 0;
  margin: 0;
}

.rail-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.rail-tabs :deep(.n-tabs-nav) {
  padding: 0 8px;
  background: var(--app-surface);
  border-bottom: 1px solid var(--taleweft-split-border);
  flex-shrink: 0;
}

.rail-tabs :deep(.n-tabs-content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.rail-tabs :deep(.n-tabs-content-wrapper) {
  height: 100%;
  overflow: hidden;
}

.rail-tabs :deep(.n-tabs-pane-wrapper) {
  height: 100%;
  overflow: hidden;
}

.rail-tabs :deep(.n-tab-pane) {
  height: 100%;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.rail-tab-scroll {
  flex: 1;
  min-height: 0;
}

.rail-icon-btn {
  max-width: 100%;
}

.primary-desk-root {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.primary-desk-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0 8px 12px;
}

.primary-desk-tabs :deep(.n-tabs-nav) {
  padding: 0 8px;
  flex-shrink: 0;
}

.primary-desk-tabs :deep(.n-tabs-pane-wrapper) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.primary-desk-tabs :deep(.n-tab-pane) {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.primary-tab-pane {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.managed-stack {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.work-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--taleweft-split-border);
}

.work-title-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.work-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.work-sub {
  font-size: 13px;
}

.autopilot-container {
  padding: 16px 20px;
  background: linear-gradient(
    to bottom,
    var(--app-surface) 0%,
    color-mix(in srgb, var(--color-success, #22c55e) 3%, var(--app-surface)) 100%
  );
  border-bottom: 1px solid var(--taleweft-split-border);
}

.monitor-container {
  height: 100%;
  padding: 20px;
  overflow-y: auto;
  background: var(--app-surface);
}

.elements-tab-wrap {
  height: 100%;
  min-height: 0;
  padding: 12px 16px 16px;
  overflow: hidden;
  background: var(--app-surface);
  display: flex;
  flex-direction: column;
}

.elements-tab-wrap :deep(.ce-panel) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.work-main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 20px;
  overflow: hidden;
}

.work-empty {
  margin-top: 80px;
}

.work-empty-prose {
  flex: 1;
  min-height: 280px;
  margin-top: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.work-empty-card {
  width: min(360px, 100%);
}

.work-empty-hint {
  font-size: 12px;
  line-height: 1.5;
}

.write-modal-body {
  padding-right: 6px;
}

.output-area {
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  color: var(--app-text-secondary);
}

.write-modal-body :deep(.n-card) {
  background: var(--card-color);
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

.write-modal-body :deep(.n-card__header) {
  padding: 12px 16px;
  font-weight: 600;
  font-size: 14px;
}

.write-modal-body :deep(.n-card__content) {
  padding: 16px;
}

.write-modal-body :deep(.n-form-item) {
  margin-bottom: 0;
}

.chapter-editor {
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 1;
  min-height: 0;
  height: 100%;
}

.editor-header {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--app-border);
}

.editor-header-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.editor-header-actions {
  flex-shrink: 0;
}

.editor-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.editor-title h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.4;
  word-break: keep-all;
  overflow-wrap: anywhere;
}

.editor-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  height: 100%;
}

.editor-body :deep(.n-input) {
  flex: 1;
  min-height: 0;
  height: 100% !important;
  max-height: none !important;
}

.editor-body :deep(.n-input .n-input-wrapper) {
  height: 100% !important;
  max-height: none !important;
  display: flex;
  flex-direction: column;
}

.editor-body :deep(.n-input__textarea-el) {
  flex: 1;
  height: 100% !important;
  min-height: 200px;
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", SimSun, serif;
  font-size: var(--manuscript-font-size, 18px);
  line-height: 1.95;
  overflow-y: auto !important;
  resize: none;
}

.editor-footer {
  padding-top: 12px;
  border-top: 1px solid var(--border-color);
}

/* 🔥 流式编辑框：编辑框本身就是流式显示 */
.editor-input-wrapper {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.editor-input-wrapper.is-streaming :deep(.n-input) {
  border-color: rgba(24, 160, 88, 0.3);
  box-shadow: 0 0 0 1px rgba(24, 160, 88, 0.1);
}

.editor-input-wrapper.is-streaming :deep(.n-input__textarea-el) {
  color: var(--app-text);
}

.streaming-cursor-overlay {
  position: absolute;
  bottom: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  background: rgba(24, 160, 88, 0.08);
  border-radius: 4px;
  pointer-events: none;
}

.streaming-cursor {
  color: #18a058;
  animation: cursor-blink-anim 1s step-end infinite;
  font-size: 14px;
}

.streaming-badge {
  font-size: 11px;
  color: #18a058;
  font-weight: 500;
}

@keyframes cursor-blink-anim {
  50% { opacity: 0; }
}

/* 🔥 流式字数动画 */
.streaming-word-count {
  color: #18a058;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.streaming-indicator {
  color: #18a058;
  font-size: 12px;
  margin-left: 4px;
  animation: cursor-blink-anim 1s step-end infinite;
}

.editor-wordcount-line {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}

.wordcount-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  margin-left: 2px;
  border-radius: 50%;
  font-size: 10px;
  font-weight: 700;
  color: var(--n-text-color-3);
  border: 1px solid var(--n-border-color);
  cursor: help;
  line-height: 1;
}

/* 当前幕信息：独占一行，避免与标题、按钮横向争抢宽度 */
.act-info-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 6px 10px;
  border-radius: var(--app-radius-sm, 8px);
  background: var(--color-brand-light);
  border: 1px solid var(--color-brand-border);
}

.act-info-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-secondary);
  line-height: 1.45;
}

.act-info-desc {
  font-size: 11px;
  color: var(--app-text-muted);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.gen-stream-meta-card {
  margin-top: 10px;
}

.plan-skel-line {
  margin-bottom: 8px;
}

.plan-skel-line:last-child {
  margin-bottom: 0;
}

.sse-log-scroll {
  max-height: 168px;
  overflow-y: auto;
  padding: 8px 10px;
  border-radius: var(--n-border-radius);
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
}

.sse-log-row {
  display: flex;
  align-items: flex-start;
  gap: 0;
}

.wa-inline-stream {
  margin-bottom: 10px;
}
/* Author desk: chrome stays quiet, the manuscript owns the center. */
.work-header { padding: 14px 24px; min-height: 68px; gap: 16px; }
.work-title { font-size: 17px; line-height: 1.5; }
.work-sub { font-size: 12px; }
.work-main { padding: 0; background: var(--app-page-bg, #f1f4f2); }
.primary-desk-tabs { padding: 0; }
.primary-desk-tabs :deep(.n-tabs-nav) { padding: 0 24px; background: var(--app-surface); }
.primary-desk-tabs :deep(.n-tabs-content) { flex: 1; min-height: 0; overflow: hidden; }
.chapter-editor { gap: 0; }
.editor-header { padding: 16px 24px; border-bottom: 1px solid var(--app-border); background: var(--app-surface); }
.editor-header-main { align-items: center; flex-wrap: wrap; }
.editor-title { flex-basis: 240px; }
.editor-title :deep(.n-input__input-el) { font-size: 17px; font-weight: 600; }
.editor-body { padding: 24px clamp(16px, 4vw, 56px) 0; }
.editor-input-wrapper { width: 100%; max-width: 840px; margin: 0 auto; background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 4px 4px 0 0; }
.editor-body :deep(.n-input) { --n-border: none; --n-border-hover: none; --n-border-focus: none; --n-box-shadow-focus: none; background: transparent; }
.editor-body :deep(.n-input .n-input-wrapper) { padding: 28px clamp(20px, 3vw, 48px); }
.editor-body :deep(.n-input__textarea-el) { letter-spacing: .035em; color: var(--app-text); caret-color: var(--color-brand); }
.editor-input-wrapper:focus-within { border-color: var(--color-brand-border, var(--color-brand)); }
.editor-footer { padding: 12px 24px; background: var(--app-surface); border-top: 1px solid var(--app-border); }
.reading-settings { width: 220px; padding: 4px 8px 20px; font-size: 13px; }
.reading-settings label { display: block; margin-bottom: 14px; }
@media (max-width: 720px) {
  .work-header { padding: 12px 16px; }
  .editor-header { padding: 12px 16px; }
  .editor-header-actions { flex-shrink: 1; }
  .editor-body { padding: 12px 8px 0; }
  .editor-body :deep(.n-input .n-input-wrapper) { padding: 20px 16px; }
  .editor-footer { padding: 10px 12px; }
  .primary-desk-tabs :deep(.n-tabs-nav) { padding: 0 16px; }
}
@media (prefers-reduced-motion: reduce) { .streaming-cursor { animation: none; } }
</style>
