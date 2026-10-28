<template>
  <n-modal
    v-model:show="modalOpen"
    :mask-closable="false"
    :close-on-esc="false"
    :closable="true"
    preset="card"
    class="novel-setup-modal"
    title="新书设置向导"
    style="width: 94%; max-width: 960px; max-height: 92vh"
    :segmented="{ content: true, footer: true }"
  >
    <n-steps :current="currentStep" :status="stepStatus" size="small" class="wizard-steps">
      <n-step title="文风 / 世界观" description="先定调，再搭 5 维框架" class="wizard-step-clickable" @click="goToStep(1)" />
      <n-step title="人物" description="主要角色" class="wizard-step-clickable" @click="goToStep(2)" />
      <n-step title="地图" description="地图系统" class="wizard-step-clickable" @click="goToStep(3)" />
      <n-step title="剧情总纲" description="故事主轴" class="wizard-step-clickable" @click="goToStep(4)" />
      <n-step title="开始" description="进入工作台" />
    </n-steps>

    <SetupDAGProgress
      :novel-id="novelId"
      :current-stage="stepStages[currentStep - 1]"
      :stages="setupStages" :run="setupRun" :busy="setupBusy" :loading="setupLoading"
      :stopping="setupStopping" :error="setupError"
      @stop="setupDag.stop" @reconnect="setupDag.resume"
    />

    <div class="step-content">
      <n-alert v-if="currentDependencyLock" type="warning" class="stage-lock-notice">
        {{ stageNames[currentDependencyLock] }}阶段尚未完成，已使用的前置资料暂时只读。请先继续{{ stageNames[currentDependencyLock] }}的审核与保存。
        <n-button secondary size="small" :disabled="isWizardGenerating || setupLoading" @click="goToStep(stepStages.indexOf(currentDependencyLock) + 1)">前往{{ stageNames[currentDependencyLock] }}继续</n-button>
      </n-alert>
      <n-alert v-else-if="currentStageLocked" type="warning" class="stage-lock-notice">
        本阶段的偏离审核、去 AI 味审核与保存回读尚未全部完成，暂不可编辑或进入下一步。已保存的分项会保留，继续生成将从上次进度恢复。
      </n-alert>
      <n-button v-if="currentStageIncomplete && !currentDependencyLock && !isWizardGenerating && !setupLoading && !openingWizard && currentStep < 4" secondary class="stage-restart-button" @click="restartSetupStage(stepStages[currentStep - 1]!)">从头重新生成</n-button>
      <div v-if="currentStageLocked && savedStagePreview.length" class="saved-stage-preview" aria-label="已保存内容，只读预览">
        <n-collapse>
          <n-collapse-item title="查看已保存内容（只读）" name="saved">
            <div v-for="item in savedStagePreview" :key="item.label" class="saved-stage-preview__item">
              <strong>{{ item.label }}</strong><p>{{ item.content }}</p>
            </div>
          </n-collapse-item>
        </n-collapse>
      </div>
      <!-- 续传提示 -->
      <n-alert v-if="resumedFromStep > 1" type="success" style="margin-bottom: 16px">
        检测到之前的进度，已回到第 {{ resumedFromStep }} 步。您可以继续完成剩余设置。
      </n-alert>

      <!-- Step 1: Generate Worldbuilding + Style (SSE) -->
      <div v-if="currentStep === 1" class="step-panel">
        <n-alert v-if="bibleError" type="error" style="margin-bottom: 16px; width: 100%">
          <div class="wizard-error-text">{{ bibleError }}</div>
        </n-alert>

        <!-- 生成中：骨架屏 + 流式数据 -->
        <div v-if="generatingBible" class="step-generating">
          <div class="generating-header">
            <div class="generating-icon">
              <n-icon size="36" color="#2080f0">
                <IconBook />
              </n-icon>
            </div>
            <div class="generating-text">
              <h3>{{ phaseMessage || '正在生成文风公约与世界观...' }}</h3>
              <p class="generating-sub">文风公约与五个维度依次生成，每项通过偏离和去 AI 味审核后保存回读。</p>
            </div>
          </div>

          <WizardSkeleton
            type="worldbuilding"
            :active-dimension="activeDimension"
            :completed-dimensions="completedDimensions"
          >
            <template #core_rules>
              <div class="dimension-fields" v-if="orderedWorldbuildingFields('core_rules').length">
                <div v-for="field in orderedWorldbuildingFields('core_rules')" :key="field.key"
                  class="field-card" :class="{ 'field-card--streaming': activeDimension === 'core_rules' && activeField === field.key }">
                  <div class="field-card__title">{{ worldbuildingFieldTitle('core_rules', field.key) }}</div>
                  <div class="field-card__content">{{ field.value }}<span v-if="activeDimension === 'core_rules' && activeField === field.key" class="streaming-cursor">▎</span></div>
                </div>
              </div>
              <div v-else-if="activeDimension === 'core_rules'" class="raw-stream-preview">
                正在生成核心法则，解析到字段后将逐项展示<span class="streaming-cursor">▎</span>
              </div>
            </template>
            <template #geography>
              <div class="dimension-fields" v-if="orderedWorldbuildingFields('geography').length">
                <div v-for="field in orderedWorldbuildingFields('geography')" :key="field.key"
                  class="field-card" :class="{ 'field-card--streaming': activeDimension === 'geography' && activeField === field.key }">
                  <div class="field-card__title">{{ worldbuildingFieldTitle('geography', field.key) }}</div>
                  <div class="field-card__content">{{ field.value }}<span v-if="activeDimension === 'geography' && activeField === field.key" class="streaming-cursor">▎</span></div>
                </div>
              </div>
              <div v-else-if="activeDimension === 'geography'" class="raw-stream-preview">
                正在生成地理生态，解析到字段后将逐项展示<span class="streaming-cursor">▎</span>
              </div>
            </template>
            <template #society>
              <div class="dimension-fields" v-if="orderedWorldbuildingFields('society').length">
                <div v-for="field in orderedWorldbuildingFields('society')" :key="field.key"
                  class="field-card" :class="{ 'field-card--streaming': activeDimension === 'society' && activeField === field.key }">
                  <div class="field-card__title">{{ worldbuildingFieldTitle('society', field.key) }}</div>
                  <div class="field-card__content">{{ field.value }}<span v-if="activeDimension === 'society' && activeField === field.key" class="streaming-cursor">▎</span></div>
                </div>
              </div>
              <div v-else-if="activeDimension === 'society'" class="raw-stream-preview">
                正在生成社会结构，解析到字段后将逐项展示<span class="streaming-cursor">▎</span>
              </div>
            </template>
            <template #culture>
              <div class="dimension-fields" v-if="orderedWorldbuildingFields('culture').length">
                <div v-for="field in orderedWorldbuildingFields('culture')" :key="field.key"
                  class="field-card" :class="{ 'field-card--streaming': activeDimension === 'culture' && activeField === field.key }">
                  <div class="field-card__title">{{ worldbuildingFieldTitle('culture', field.key) }}</div>
                  <div class="field-card__content">{{ field.value }}<span v-if="activeDimension === 'culture' && activeField === field.key" class="streaming-cursor">▎</span></div>
                </div>
              </div>
              <div v-else-if="activeDimension === 'culture'" class="raw-stream-preview">
                正在生成历史文化，解析到字段后将逐项展示<span class="streaming-cursor">▎</span>
              </div>
            </template>
            <template #daily_life>
              <div class="dimension-fields" v-if="orderedWorldbuildingFields('daily_life').length">
                <div v-for="field in orderedWorldbuildingFields('daily_life')" :key="field.key"
                  class="field-card" :class="{ 'field-card--streaming': activeDimension === 'daily_life' && activeField === field.key }">
                  <div class="field-card__title">{{ worldbuildingFieldTitle('daily_life', field.key) }}</div>
                  <div class="field-card__content">{{ field.value }}<span v-if="activeDimension === 'daily_life' && activeField === field.key" class="streaming-cursor">▎</span></div>
                </div>
              </div>
              <div v-else-if="activeDimension === 'daily_life'" class="raw-stream-preview">
                正在生成沉浸感细节，解析到字段后将逐项展示<span class="streaming-cursor">▎</span>
              </div>
            </template>
          </WizardSkeleton>

          <!-- 文风公约实时预览（SSE 生成中即可见） -->
          <div v-if="styleText" class="style-preview-generating">
            <div class="style-preview-header">
              <n-icon size="16" color="#18a058"><IconCheck /></n-icon>
              <span class="style-preview-title">文风公约</span>
              <n-tag size="tiny" type="success">已生成</n-tag>
            </div>
            <div class="style-preview-content">{{ styleText }}</div>
          </div>
        </div>

        <!-- 生成完成后显示可编辑预览 -->
        <div v-else-if="bibleGenerated && !currentStageLocked" class="bible-preview">
          <n-alert type="success" title="文风公约与世界观生成完成" style="margin-bottom: 16px">
            请查看并修改文风公约和世界观设定，确认后下一步将基于此生成人物和地点。
          </n-alert>

          <n-collapse :default-expanded-names="['style', 'worldbuilding']">
            <n-collapse-item title="文风公约" name="style">
              <n-card size="small">
                <n-input
                  v-model:value="styleText"
                  type="textarea"
                  :autosize="{ minRows: 3, maxRows: 10 }"
                  placeholder="文风公约"
                />
              </n-card>
            </n-collapse-item>

            <n-collapse-item title="世界观（5维度框架）" name="worldbuilding">
              <n-space vertical size="small">
                <n-card v-for="dim in wbDimensionCards" :key="dim.key" size="small" :title="dim.label">
                  <div class="dimension-fields">
                    <div v-for="field in orderedWorldbuildingFields(dim.key, { includeEmpty: true })" :key="field.key" class="field-card field-card--editable">
                      <div class="field-card__title">{{ worldbuildingFieldTitle(dim.key, field.key) }}</div>
                      <n-input
                        v-model:value="worldbuildingData[dim.key][field.key]"
                        type="textarea"
                        :autosize="{ minRows: 1, maxRows: 4 }"
                        size="small"
                      />
                    </div>
                  </div>
                </n-card>
              </n-space>
            </n-collapse-item>
          </n-collapse>
          <n-button secondary style="margin-top: 12px" @click="restartSetupStage('worldbuilding')">
            重新生成
          </n-button>
        </div>

        <!-- 初始状态 -->
        <div v-else-if="!currentDependencyLock" class="step-info">
          <n-icon size="48" color="#18a058">
            <IconBook />
          </n-icon>
          <h3>准备生成文风公约与世界观</h3>
          <p>AI 将先生成文风公约，再逐维度构建世界观。</p>
          <n-button type="primary" style="margin-top: 16px" @click="startBibleGeneration()">
            {{ stageStartLabel('worldbuilding') }}
          </n-button>
        </div>
      </div>

      <!-- Step 2: Generate Characters (SSE) -->
      <div v-else-if="currentStep === 2" class="step-panel">
        <n-alert v-if="charactersError" type="error" style="margin-bottom: 16px; width: 100%">
          {{ charactersError }}
        </n-alert>

        <!-- 生成中：逐个角色流式呈现 -->
        <div v-if="generatingCharacters && !charactersGenerated" class="step-generating">
          <div class="generating-header">
            <div class="generating-icon">
              <n-icon size="36" color="#2080f0">
                <IconPeople />
              </n-icon>
            </div>
            <div class="generating-text">
              <h3>{{ phaseMessage || '正在生成人物...' }}</h3>
              <p class="generating-sub">完成生成与校验后显示可编辑的人物设定</p>
            </div>
          </div>

          <div class="streaming-cards">
            <!-- 已接收的角色 —— 完整卡片 -->
            <transition-group name="fade-slide">
              <div v-for="(char, idx) in streamingCharacters" :key="char.name || idx" class="char-card char-card--filled">
                <div class="char-card__header">
                  <div class="char-card__avatar" :class="char.role === '主角' ? 'char-card__avatar--protag' : ''">{{ char.name?.[0] || '?' }}</div>
                  <div class="char-card__title">
                    <span class="char-card__name">{{ char.name }}</span>
                    <n-tag size="small" :type="char.role === '主角' ? 'success' : 'default'" round>{{ char.role || '角色' }}</n-tag>
                  </div>
                </div>
                <div v-if="char.description" class="char-card__desc">{{ char.description }}</div>
                <div v-if="char.core_belief" class="char-card__anchor">
                  <span class="char-card__anchor-label">核心信念</span>
                  <span>{{ char.core_belief }}</span>
                </div>
                <div v-if="char.verbal_tic || char.idle_behavior" class="char-card__anchor">
                  <span class="char-card__anchor-label">声线/动作</span>
                  <span>{{ [char.verbal_tic, char.idle_behavior].filter(Boolean).join('；') }}</span>
                </div>
                <div v-if="char.relationships && char.relationships.length" class="char-card__relations">
                  <n-tag v-for="(rel, ri) in char.relationships.slice(0, 3)" :key="ri" size="tiny" :bordered="false" type="info">
                    {{ typeof rel === 'string' ? rel : (rel.relation || rel.description || rel.target || '') }}
                  </n-tag>
                </div>
              </div>
            </transition-group>
            <!-- 当前正在生成的骨架位 —— 与卡片结构一致 -->
            <div class="char-card char-card--loading">
              <div class="char-card__header">
                <div class="char-card__avatar char-card__avatar--skeleton">
                  <span class="skeleton-dot__pulse"></span>
                </div>
                <div class="char-card__title">
                  <span class="char-card__skeleton-bar" style="width: 60px"></span>
                  <span class="char-card__skeleton-bar char-card__skeleton-bar--tag"></span>
                </div>
              </div>
              <div class="char-card__skeleton-body">
                <span class="char-card__skeleton-bar" style="width: 90%"></span>
                <span class="char-card__skeleton-bar" style="width: 70%"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- 生成完成后显示可编辑预览 -->
        <div v-else-if="charactersGenerated && !currentStageLocked" class="bible-preview">
          <n-alert type="success" title="人物生成完成" style="margin-bottom: 16px">
            请查看并修改角色设定，确认后将继续。
          </n-alert>
          <n-space vertical size="small" style="margin-bottom: 14px">
            <n-button
              size="small"
              type="primary"
              secondary
              :loading="bulkExtractingPsyche"
              :disabled="!editableCharacters.length"
              @click="runBulkCharacterExtract"
            >
              从简介填充空锚点（无模型）
            </n-button>
            <n-text depth="3" style="font-size: 11px; line-height: 1.5">
              与工作台「角色锚点」同一套 Bible 字段；仅填补仍为空的 T0 / 声线风格等，不覆盖已写内容。可在下方改完再点「确认修改并继续」落库。
            </n-text>
          </n-space>
          <n-list bordered class="character-editor-list">
            <n-list-item v-for="(char, idx) in editableCharacters" :key="idx">
              <div class="editable-character">
                <n-space vertical size="small" style="width: 100%">
                  <div class="character-editor-head">
                    <n-input v-model:value="char.name" size="small" class="character-editor-head__name" placeholder="姓名" />
                    <n-input v-model:value="char.role" size="small" class="character-editor-head__role" placeholder="角色定位" />
                    <n-button quaternary size="small" type="error" @click="editableCharacters.splice(idx, 1)">删除</n-button>
                  </div>

                  <n-grid :cols="2" :x-gap="10" :y-gap="10" responsive="screen">
                    <n-grid-item>
                      <div class="role-lock-panel">
                        <div class="role-lock-panel__title">基础</div>
                        <div class="character-meta-grid">
                          <n-input v-model:value="char.gender" size="small" placeholder="性别/呈现" />
                          <n-input v-model:value="char.age" size="small" placeholder="年龄/年龄段" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">功能定位</div>
                          <n-input v-model:value="char.description" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">外貌锚点</div>
                          <n-input v-model:value="char.appearance" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">性格底色</div>
                          <n-input v-model:value="char.personality" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">公开人设</div>
                          <n-input v-model:value="char.public_profile" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                      </div>
                    </n-grid-item>

                    <n-grid-item>
                      <div class="role-lock-panel role-lock-panel--strong">
                        <div class="role-lock-panel__title">写作锁</div>
                        <div class="editable-field">
                          <div class="editable-field__label">核心信念</div>
                          <n-input v-model:value="char.core_belief" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">核心驱动力</div>
                          <n-input v-model:value="char.core_motivation" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">内在缺口</div>
                          <n-input v-model:value="char.inner_lack" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">道德禁忌</div>
                          <n-dynamic-tags v-model:value="char.moral_taboos" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">心理状态</div>
                          <n-input v-model:value="char.mental_state" size="small" placeholder="例如：警惕、愧疚、亢奋" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">状态成因</div>
                          <n-input v-model:value="char.mental_state_reason" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                      </div>
                    </n-grid-item>

                    <n-grid-item>
                      <div class="role-lock-panel">
                        <div class="role-lock-panel__title">声线与动作</div>
                        <div class="editable-field">
                          <div class="editable-field__label">口头禅</div>
                          <n-input v-model:value="char.verbal_tic" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">压力动作</div>
                          <n-input v-model:value="char.idle_behavior" size="small" />
                        </div>
                        <div class="voice-grid">
                          <n-input v-model:value="char.voice_profile.style" size="small" placeholder="声线风格" />
                          <n-input v-model:value="char.voice_profile.sentence_pattern" size="small" placeholder="句式模式" />
                          <n-input v-model:value="char.voice_profile.speech_tempo" size="small" placeholder="语速" />
                        </div>
                      </div>
                    </n-grid-item>

                    <n-grid-item>
                      <div class="role-lock-panel">
                        <div class="role-lock-panel__title">隐藏线索</div>
                        <div class="editable-field">
                          <div class="editable-field__label">隐藏身份 / 真实动机</div>
                          <n-input v-model:value="char.hidden_profile" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">背景经历</div>
                          <n-input v-model:value="char.background" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" size="small" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">揭示章节</div>
                          <n-input-number v-model:value="char.reveal_chapter" size="small" :min="1" clearable style="width: 100%" />
                        </div>
                        <div class="editable-field">
                          <div class="editable-field__label">人物关系</div>
                          <div class="relationship-editor">
                            <div v-for="(rel, ri) in char.relationships" :key="ri" class="relationship-row">
                              <n-input
                                v-model:value="rel.target"
                                size="small"
                                placeholder="目标人物"
                              />
                              <n-input
                                v-model:value="rel.relation"
                                size="small"
                                placeholder="关系类型"
                              />
                              <n-input
                                v-model:value="rel.description"
                                size="small"
                                placeholder="张力说明"
                              />
                              <n-button quaternary size="small" type="error" @click="char.relationships.splice(ri, 1)">删除</n-button>
                            </div>
                            <n-button size="small" secondary @click="addRelationship(char)">添加关系</n-button>
                          </div>
                        </div>
                      </div>
                    </n-grid-item>

                    <n-grid-item :span="2" v-if="char.active_wounds.length">
                      <div class="role-lock-panel">
                        <div class="role-lock-panel__title">创伤触发器</div>
                        <div class="wound-grid">
                          <div v-for="(wound, wi) in char.active_wounds" :key="wi" class="wound-row">
                            <n-input v-model:value="wound.description" size="small" placeholder="创伤" />
                            <n-input v-model:value="wound.trigger" size="small" placeholder="触发条件" />
                            <n-input v-model:value="wound.effect" size="small" placeholder="触发反应" />
                          </div>
                        </div>
                      </div>
                    </n-grid-item>
                  </n-grid>
                </n-space>
              </div>
            </n-list-item>
          </n-list>
          <n-button secondary style="margin-top: 12px" @click="restartSetupStage('characters')">
            重新生成
          </n-button>
        </div>

        <!-- 初始状态 -->
        <div v-else-if="!currentDependencyLock" class="step-info">
          <n-icon size="48" color="#2080f0">
            <IconPeople />
          </n-icon>
          <h3>生成主要角色</h3>
          <p>基于已确认的世界观，AI 将生成主要角色及其关系。</p>
          <n-button type="primary" style="margin-top: 16px" @click="startCharactersGeneration()">
            {{ stageStartLabel('characters') }}
          </n-button>
        </div>
      </div>

      <!-- Step 3: Generate Locations (SSE) -->
      <div v-else-if="currentStep === 3" class="step-panel">
        <n-alert v-if="locationsError" type="error" style="margin-bottom: 16px; width: 100%">
          {{ locationsError }}
        </n-alert>

        <!-- 生成中：骨架屏 + 流式数据 -->
        <div v-if="generatingLocations && !locationsGenerated" class="step-generating">
          <div class="generating-header">
            <div class="generating-icon">
              <n-icon size="36" color="#f0a020">
                <IconMap />
              </n-icon>
            </div>
            <div class="generating-text">
              <h3>{{ phaseMessage || '正在生成地图...' }}</h3>
              <p class="generating-sub">
                {{ streamingLocations.length
                  ? `已收到 ${streamingLocations.length} 个有效地点，地图仍在生成与校验。`
                  : 'AI 正在构思地点与地图结构，完成后显示预览。' }}
              </p>
            </div>
          </div>

          <div class="streaming-loc-cards">
            <!-- 已接收的地点 —— 完整卡片 -->
            <transition-group name="fade-slide">
              <div v-for="(loc, idx) in streamingLocations" :key="loc.name || loc.id || idx" class="loc-card loc-card--filled">
                <div class="loc-card__header">
                  <div class="loc-card__icon">📍</div>
                  <div class="loc-card__title">
                    <span class="loc-card__name">{{ loc.name }}</span>
                    <n-tag size="small" type="info" round>{{ loc.type || loc.location_type || '地点' }}</n-tag>
                  </div>
                </div>
                <div v-if="loc.description" class="loc-card__desc">{{ loc.description }}</div>
              </div>
            </transition-group>
            <!-- 当前正在生成的骨架位 -->
            <div class="loc-card loc-card--loading">
              <div class="loc-card__header">
                <div class="loc-card__icon--skeleton"></div>
                <div class="loc-card__title">
                  <span class="loc-card__skeleton-bar" style="width: 70px"></span>
                  <span class="loc-card__skeleton-bar" style="width: 40px; height: 20px; border-radius: 10px"></span>
                </div>
              </div>
              <div class="loc-card__skeleton-body">
                <span class="loc-card__skeleton-bar" style="width: 85%"></span>
                <span class="loc-card__skeleton-bar" style="width: 60%"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- 生成完成后显示可编辑预览 -->
        <div v-else-if="locationsGenerated && !currentStageLocked" class="bible-preview">
          <n-alert type="success" title="地图生成完成" style="margin-bottom: 16px">
            请查看并修改地点设定，确认后将继续。
          </n-alert>
          <BibleLocationsGraphPreview :locations="bibleData.locations || []" />
          <n-list bordered style="margin-top: 16px">
            <n-list-item v-for="(loc, idx) in editableLocations" :key="loc.id || idx">
              <div class="editable-location">
                <n-space vertical size="small" style="width: 100%">
                  <n-space :size="8" align="center">
                    <n-input v-model:value="loc.name" size="small" style="width: 140px" placeholder="地点名" />
                    <n-input v-model:value="loc.location_type" size="small" style="width: 100px" placeholder="类型" />
                    <n-button quaternary size="small" type="error" @click="editableLocations.splice(idx, 1)">删除</n-button>
                  </n-space>
                  <n-input
                    v-model:value="loc.description"
                    type="textarea"
                    :autosize="{ minRows: 1, maxRows: 4 }"
                    size="small"
                    placeholder="地点描述"
                  />
                </n-space>
              </div>
            </n-list-item>
          </n-list>
          <n-button secondary style="margin-top: 12px" @click="restartSetupStage('locations')">
            重新生成
          </n-button>
        </div>

        <!-- 初始状态 -->
        <div v-else-if="!currentDependencyLock" class="step-info">
          <n-icon size="48" color="#f0a020">
            <IconMap />
          </n-icon>
          <h3>生成地图系统</h3>
          <p>基于已确认的世界观和人物，AI 将生成重要地点和地图结构。</p>
          <n-button type="primary" style="margin-top: 16px" @click="startLocationsGeneration()">
            {{ stageStartLabel('locations') }}
          </n-button>
        </div>
      </div>

      <!-- Step 4: 剧情总纲（LLM 推演） -->
      <div v-else-if="currentStep === 4" class="step-panel step-panel--storyline">
        <n-alert
          v-if="step4RestoredFromCache"
          type="success"
          closable
          class="wizard-hint-alert"
          style="margin-bottom: 12px; width: 100%"
          @close="step4RestoredFromCache = false"
        >
          已恢复上次生成的<strong>剧情总纲</strong>预览（本地缓存，减少重复生成）。
        </n-alert>
        <div class="step-info step-info--wide">
          <n-icon size="48" color="#2080f0">
            <IconTimeline />
          </n-icon>
          <h3>生成剧情总纲</h3>
          <p>基于你已确认的世界观、人物与地图，系统会生成一份完整的<strong>剧情总纲</strong>，包含主线概述、阶段规划、核心冲突与预期结局。</p>
        </div>

        <n-alert v-if="plotOutlineError" type="error" style="margin-bottom: 12px; width: 100%">
          {{ plotOutlineError }}
        </n-alert>
        <n-alert v-if="plotOutlineCommitted" type="success" title="已保存剧情总纲" style="margin-bottom: 12px; width: 100%">
          剧情总纲已保存，可供后续宏观规划与章节规划直接读取。
        </n-alert>

        <div v-if="plotOutlineBusy && !plotOutline" class="step-generating plot-outline-generating">
          <div class="generating-header">
            <div class="generating-icon">
              <n-icon size="36" color="#2080f0">
                <IconTimeline />
              </n-icon>
            </div>
            <div class="generating-text">
              <h3>{{ plotOutlineStatusMessage }}</h3>
              <p class="generating-sub">正在汇总已确认设定并生成可编辑的故事主轴</p>
            </div>
          </div>

          <div class="plot-outline-progress">
            <div
              v-for="item in plotOutlineProgressItems"
              :key="item.key"
              class="plot-outline-progress__item"
              :class="`plot-outline-progress__item--${item.state}`"
            >
              <div class="plot-outline-progress__dot"></div>
              <div class="plot-outline-progress__body">
                <div class="plot-outline-progress__label">{{ item.label }}</div>
                <div class="plot-outline-progress__desc">{{ item.desc }}</div>
              </div>
            </div>
          </div>

          <WizardSkeleton type="storyline" />

          <div v-if="plotOutlineLivePreview" class="raw-stream-preview plot-outline-live-preview">
            {{ plotOutlineLivePreview }}<span class="streaming-cursor">▎</span>
          </div>

          <n-space v-if="featureFlags.aiInvocationDebug && plotOutlineSessionId" justify="center">
            <n-button secondary @click="openPlotOutlineReviewPanel(plotOutlineSessionId)">
              打开 AI 审阅
            </n-button>
          </n-space>
        </div>

        <div v-else class="plot-options-block">
            <n-space vertical :size="12" style="width: 100%">
              <n-card v-if="plotOutline && !currentStageLocked" size="small" :bordered="true" class="plot-option-card">
                <template #header>
                  <n-space align="center" :size="8">
                    <n-tag size="small" type="info" round>剧情总纲</n-tag>
                    <span class="plot-option-title">完整主线规划</span>
                  </n-space>
                </template>
                <n-space vertical :size="12">
                  <div class="plot-outline-editor">
                    <div
                      v-for="key in plotOutlineTopFieldKeys"
                      :key="key"
                      class="plot-kv-field"
                    >
                      <div class="plot-kv-label">{{ plotFieldLabel(key) }}</div>
                      <n-input
                        :value="plotFieldText(editablePlotOutline, key)"
                        type="textarea"
                        :autosize="{ minRows: key === 'main_story_overview' ? 4 : 3, maxRows: 8 }"
                        :placeholder="`填写${plotFieldLabel(key)}`"
                        @update:value="updatePlotField(editablePlotOutline, key, $event)"
                      />
                    </div>
                  </div>
                  <div v-if="editablePlotOutline.stage_plan?.length" class="plot-subline-list plot-outline-stage-editor">
                    <div class="plot-subline-title">阶段规划</div>
                    <div v-for="(stage, index) in editablePlotOutline.stage_plan" :key="stage.phase || index" class="plot-subline-item plot-stage-edit-item">
                      <div class="plot-stage-edit-header">
                        <n-tag size="tiny" type="default" round>{{ stage.label }}</n-tag>
                        <span class="plot-subline-name">{{ stageRangePercentLabel(stage) }}</span>
                      </div>
                      <div class="plot-stage-chapter-row">
                        <n-input-number
                          :value="stage.chapter_start ?? null"
                          :min="1"
                          :precision="0"
                          placeholder="起始章"
                          @update:value="updateStageChapterNumber(index, 'chapter_start', $event)"
                        />
                        <span class="plot-stage-chapter-separator">至</span>
                        <n-input-number
                          :value="stage.chapter_end ?? null"
                          :min="1"
                          :precision="0"
                          placeholder="结束章"
                          @update:value="updateStageChapterNumber(index, 'chapter_end', $event)"
                        />
                        <span class="plot-subline-purpose">章</span>
                      </div>
                      <div
                        v-for="key in stageContentFieldKeys(stage)"
                        :key="key"
                        class="plot-kv-field"
                      >
                        <div class="plot-kv-label">{{ plotFieldLabel(key) }}</div>
                        <n-input
                          :value="plotFieldText(stage, key)"
                          type="textarea"
                          :autosize="{ minRows: 3, maxRows: 7 }"
                          :placeholder="`填写${plotFieldLabel(key)}`"
                          @update:value="updatePlotField(stage, key, $event)"
                        />
                      </div>
                    </div>
                  </div>
                </n-space>
              </n-card>
            </n-space>

            <n-space style="margin-top: 16px; width: 100%" justify="center" :size="12">
              <n-button v-if="currentStageLocked" type="primary" :disabled="plotOutlineBusy" @click="startSetupStage('plot_outline')">
                继续生成
              </n-button>
              <n-button secondary :disabled="plotOutlineBusy" @click="refreshPlotOutline">
                从头重新生成
              </n-button>
              <n-button
                v-if="featureFlags.aiInvocationDebug && plotOutlineSessionId"
                secondary
                :disabled="plotOutlineBusy"
                @click="openPlotOutlineReviewPanel(plotOutlineSessionId)"
              >
                打开 AI 审阅
              </n-button>
            </n-space>
          </div>
      </div>

      <!-- Step 5: Complete -->
      <div v-else-if="currentStep === 5" class="step-panel">
        <div class="step-info">
          <n-icon size="48" color="#18a058">
            <IconCheck />
          </n-icon>
          <h3>准备就绪！</h3>
          <p>所有基础设置已完成，现在可以开始创作了。</p>
          <p style="margin-top: 12px; color: #666">您可以随时在工作台的"设置"面板中调整这些内容。</p>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="wizard-footer">
        <div class="wizard-footer__group">
          <n-button v-if="currentStep > 1 && currentStep < 5" :disabled="isWizardGenerating" @click="handlePrev">
            上一步
          </n-button>
          <n-button v-if="currentStep > 1 && currentStep < 5" @click="requestClose">
            稍后继续
          </n-button>
        </div>
        <div class="wizard-footer__group wizard-footer__group--primary">
          <!-- 步骤1~3：已生成后显示"确认修改并继续" -->
          <n-button
            v-if="!currentDependencyLock && ((currentStep === 1 && bibleGenerated) || (currentStep === 2 && charactersGenerated) || (currentStep === 3 && locationsGenerated))"
            type="primary"
            :loading="savingStep"
            :disabled="isWizardGenerating || currentStageLocked || setupLoading"
            @click="handleNext"
          >
            确认修改并继续
          </n-button>
          <n-button
            v-if="currentStep === 4"
            type="primary"
            :loading="savingStep"
            :disabled="!plotOutline || isWizardGenerating || currentStageLocked || setupLoading"
            @click="handleNext"
          >
            确认修改并继续
          </n-button>
          <!-- 步骤5：进入工作台 -->
          <n-button v-if="currentStep === 5" type="primary" @click="handleComplete">
            进入工作台
          </n-button>
        </div>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { h, ref, watch, computed, onMounted, onUnmounted } from 'vue'
import { useMessage, useDialog } from 'naive-ui'
import { bibleApi, type BibleDTO } from '@/api/bible'
// timeout constants removed - SSE runs until complete or error
import { worldbuildingApi } from '@/api/worldbuilding'
import { workflowApi, type PlotOutlineDTO } from '@/api/workflow'
import type { InvocationResponseDTO, InvocationVariableBinding } from '@/api/aiInvocation'
import { characterPsycheApi } from '@/api/engineCore'
import { featureFlags } from '@/config/features'
import { getWorldbuildingDimensionLabel } from '@/domain/worldbuilding/contract'
import { formatApiError, isLikelyTimeoutError } from '@/utils/apiError'
import {
  WB_DIMS,
  createEmptyBible,
  emptyWorldbuildingShape,
  hasWorldbuildingContent,
  mergeWorldbuildingDisplay,
  normalizeWorldbuildingFromApi,
  orderedWorldbuildingFields as buildOrderedWorldbuildingFields,
  styleConventionFromBible,
  worldbuildingFieldTitle,
  worldbuildingFromWorldSettings,
  type WorldbuildingDimKey,
  type WorldbuildingDraftShape,
} from '@/onboarding/bibleSetupModel'
import {
  characterDraftKey,
  createEmptyRelationship,
  formatCharacterDescriptionForSave,
  formatRelationship,
  mapCharacterToEditable,
  mapGeneratedCharacterToEditable,
  serializeRelationships,
  type EditableCharacter,
  type GeneratedCharacterPayload,
} from '@/onboarding/characterSetupModel'
import {
  buildEditablePlotOutlinePayload as buildPlotOutlinePayload,
  buildStageRangePercentLabel,
  clonePlotOutline,
  createEmptyPlotOutline,
  extractPlotOutlineFromResult,
  getPlotOutlineTopFieldKeys,
  normalizePlotOutlineShape,
  plotFieldLabel,
  plotFieldText,
  stageContentFieldKeys,
  updatePlotField,
  validateEditablePlotOutline,
  type PlotOutlineProgressItem,
  type PlotOutlineProgressState,
  type PlotOutlineStatus,
} from '@/onboarding/plotOutlineModel'
import { useAIInvocationStore } from '@/stores/aiInvocationStore'
import { extractBoundOutputMaps, parseJsonLikeRecord } from '@/utils/invocationOutput'
import { invocationFailureMessage } from '@/utils/invocationFailure'
import BibleLocationsGraphPreview from './BibleLocationsGraphPreview.vue'
import WizardSkeleton from './WizardSkeleton.vue'
import SetupDAGProgress from './SetupDAGProgress.vue'
import { useSetupDag } from '@/composables/useSetupDag'
import type { SetupStageId } from '@/api/setupDag'
import {
  clearWizardUiCache,
  isPlotOutlineCacheFresh,
  markWizardCompleted,
  readWizardUiCache,
  setWizardLastStep,
  writeWizardUiCache,
  type WizardUiCachePayload,
} from '@/utils/wizardStageCache'

function orderedWorldbuildingFields(
  dim: WorldbuildingDimKey,
  opts: { includeEmpty?: boolean } = {},
): Array<{ key: string; value: string }> {
  return buildOrderedWorldbuildingFields(worldbuildingData.value, dim, opts)
}

function mergeWorldbuildingIntoCurrent(
  next: WorldbuildingDraftShape,
  opts: { markCompleted?: boolean } = {},
) {
  if (!hasWorldbuildingContent(next)) return
  worldbuildingData.value = mergeWorldbuildingDisplay(next, worldbuildingData.value)
  if (opts.markCompleted === false) return
  completedDimensions.value = new Set([
    ...completedDimensions.value,
    ...WB_DIMS.filter(dim => Object.values(next[dim]).some(value => String(value || '').trim())),
  ])
}

function applyWorldbuildingRecord(record: Record<string, unknown>) {
  const normalized = normalizeWorldbuildingFromApi(record)
  mergeWorldbuildingIntoCurrent(normalized)
  const style = String(record.style ?? '').trim()
  if (style) styleText.value = style
}

function applyWorldbuildingBoundOutputs(record: Record<string, unknown>, bindings: InvocationVariableBinding[]) {
  if (!bindings.length) return
  const { byVariableKey } = extractBoundOutputMaps(record, bindings)
  const boundRecord: Record<string, unknown> = {}
  const style = byVariableKey['worldbuilding.style']
  if (style) boundRecord.style = style
  const content = byVariableKey['worldbuilding.content']
  if (content !== undefined) boundRecord.worldbuilding = content
  for (const dim of WB_DIMS) {
    const value = byVariableKey[`worldbuilding.${dim}`]
    if (value !== undefined) boundRecord[dim] = value
  }
  if (Object.keys(boundRecord).length) applyWorldbuildingRecord(boundRecord)
}

function applyBibleInvocationPreview(stage: 'worldbuilding' | 'characters' | 'locations', payload: InvocationResponseDTO) {
  const content = payload.attempt?.content || ''
  const record = parseJsonLikeRecord(content)
  if (!record) return
  if (stage === 'worldbuilding') {
    const bindings = payload.session?.output_bindings || payload.session?.variable_plan?.bindings || []
    applyWorldbuildingRecord(record)
    applyWorldbuildingBoundOutputs(record, bindings)
    return
  }
  if (stage === 'characters') {
    const rows = Array.isArray(record.characters) ? record.characters : []
    const next = rows
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && String(item.name || '').trim()))
      .map(item => mapGeneratedCharacterToEditable(item as GeneratedCharacterPayload))
    streamingCharacters.value = next
    generatedCharacterDrafts.value = Object.fromEntries(next.map(item => [
      characterDraftKey({ id: item.id, name: item.name }),
      item,
    ]).filter(([key]) => Boolean(key)))
    if (next.length) phaseMessage.value = `已生成 ${next.length} 个角色，正在等待模型完成...`
    return
  }
  if (stage !== 'locations') return

  // The setup endpoint hands the long-running provider call to the invocation
  // store. Keep the location step useful while that call is in flight by
  // rendering complete objects from the persisted partial JSON response. This
  // is replace-based: polling must never duplicate cards as more chunks arrive.
  const rows = Array.isArray(record.locations) ? record.locations : []
  const next = rows
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && String(item.name || '').trim()))
    .map(item => ({
      name: String(item.name || '').trim(),
      id: item.id == null ? undefined : String(item.id),
      type: item.type == null ? undefined : String(item.type),
      location_type: item.location_type == null ? undefined : String(item.location_type),
      description: String(item.description || ''),
    }))
  streamingLocations.value = next
  if (next.length) phaseMessage.value = `已生成 ${next.length} 个地点，正在等待地图完成...`
}

function applyWorldbuildingChunk(chunk: string) {
  if (!chunk) return
  worldbuildingRawStream.value += chunk
}

const IconBook = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', fill: 'currentColor' },
    h('path', { d: 'M18 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zM6 4h5v8l-2.5-1.5L6 12V4z' })
  )

const IconPeople = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', fill: 'currentColor' },
    h('path', { d: 'M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z' })
  )

const IconMap = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', fill: 'currentColor' },
    h('path', { d: 'M20.5 3l-.16.03L15 5.1 9 3 3.36 4.9c-.21.07-.36.25-.36.48V20.5c0 .28.22.5.5.5l.16-.03L9 18.9l6 2.1 5.64-1.9c.21-.07.36-.25.36-.48V3.5c0-.28-.22-.5-.5-.5zM15 19l-6-2.11V5l6 2.11V19z' })
  )

const IconTimeline = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', fill: 'currentColor' },
    h('path', { d: 'M23 8c0 1.1-.9 2-2 2-.18 0-.35-.02-.51-.07l-3.56 3.55c.05.16.07.34.07.52 0 1.1-.9 2-2 2s-2-.9-2-2c0-.18.02-.36.07-.52l-2.55-2.55c-.16.05-.34.07-.52.07s-.36-.02-.52-.07l-4.55 4.56c.05.16.07.33.07.51 0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2c.18 0 .35.02.51.07l4.56-4.55C8.02 9.36 8 9.18 8 9c0-1.1.9-2 2-2s2 .9 2 2c0 .18-.02.36-.07.52l2.55 2.55c.16-.05.34-.07.52-.07s.36.02.52.07l3.55-3.56C19.02 8.35 19 8.18 19 8c0-1.1.9-2 2-2s2 .9 2 2z' })
  )

const IconCheck = () =>
  h(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', fill: 'currentColor' },
    h('path', { d: 'M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z' })
  )

const props = withDefaults(
  defineProps<{
    novelId: string
    show: boolean
    targetChapters?: number
  }>(),
  { targetChapters: 100 }
)

const message = useMessage()
const aiInvocationStore = useAIInvocationStore()
let mainPlotSessionUnsub: (() => void) | null = null
const bibleInvocationUnsubs = new Map<string, () => void>()
const bibleStageSessions = new Map<string, string>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'complete'): void
  (e: 'skip'): void
}>()

const modalOpen = computed({
  get: () => props.show,
  set: (v: boolean) => {
    if (v) {
      emit('update:show', true)
      return
    }
    requestClose()
  },
})

const currentStep = ref(1)
const stepStatus = ref<'process' | 'finish' | 'error' | 'wait'>('process')
const resumedFromStep = ref(0)

// ── 第1步：SSE 流式生成世界观 ──
const generatingBible = ref(false)
const bibleGenerated = ref(false)
const bibleError = ref('')
const bibleData = ref<BibleDTO>(createEmptyBible())
const worldbuildingData = ref<WorldbuildingDraftShape>(emptyWorldbuildingShape())
const styleText = ref('')

/** SSE 流式状态 */
const phaseMessage = ref('')
const activeDimension = ref('')
const completedDimensions = ref<Set<string>>(new Set())
const activeField = ref('')
const arrivedFields = ref<Set<string>>(new Set())
const sseAbortController = ref<AbortController | null>(null)
const worldbuildingRawStream = ref('')

const styleConventionDisplay = computed(() => {
  if (styleText.value) return styleText.value
  return styleConventionFromBible(bibleData.value)
})

/** 世界观维度卡片（用于生成完后的折叠面板） */
const wbDimensionCards = computed(() => {
  return WB_DIMS.map(key => ({
    key,
    label: getWorldbuildingDimensionLabel(key),
    data: worldbuildingData.value[key],
  }))
})

// ── 第2步：SSE 流式生成人物 ──
const generatingCharacters = ref(false)
const charactersGenerated = ref(false)
const charactersError = ref('')
const streamingCharacters = ref<Array<Partial<EditableCharacter> & { name: string; role: string; description: string }>>([])
const charactersSseAbort = ref<AbortController | null>(null)
const generatedCharacterDrafts = ref<Record<string, Partial<EditableCharacter>>>({})

/** 可编辑的人物列表（从 bibleData 拷贝，用户可修改后确认落库） */
function addRelationship(char: EditableCharacter): void {
  char.relationships.push(createEmptyRelationship())
}

const editableCharacters = ref<EditableCharacter[]>([])

// ── 第3步：SSE 流式生成地点 ──
const generatingLocations = ref(false)
const locationsGenerated = ref(false)
const locationsError = ref('')
const streamingLocations = ref<Array<{ name: string; id?: string; type?: string; location_type?: string; description: string }>>([])
const locationsSseAbort = ref<AbortController | null>(null)
/** 可编辑的地点列表（从 bibleData 拷贝，用户可修改后确认落库） */
const editableLocations = ref<Array<{ name: string; id?: string; parent_id?: string | null; location_type?: string; description: string }>>([])

function setBibleStageReviewWaiting(stage: string, waiting: boolean) {
  if (stage === 'worldbuilding') {
    generatingBible.value = waiting
    bibleGenerated.value = false
    activeDimension.value = ''
    activeField.value = ''
    completedDimensions.value = new Set()
  } else if (stage === 'characters') {
    generatingCharacters.value = waiting
    charactersGenerated.value = false
  } else if (stage === 'locations') {
    generatingLocations.value = waiting
    locationsGenerated.value = false
  }
  phaseMessage.value = waiting ? '等待 AI 审阅批准...' : ''
}

function setBibleStageHeadlessGenerating(stage: string) {
  if (stage === 'worldbuilding') {
    generatingBible.value = true
    bibleGenerated.value = false
    activeDimension.value = ''
    activeField.value = ''
    completedDimensions.value = new Set()
    phaseMessage.value = '正在生成文风公约与世界观...'
  } else if (stage === 'characters') {
    generatingCharacters.value = true
    charactersGenerated.value = false
    phaseMessage.value = '正在生成人物...'
  } else if (stage === 'locations') {
    generatingLocations.value = true
    locationsGenerated.value = false
    phaseMessage.value = '正在生成地点...'
  }
}

function failBibleStage(stage: string, error: string) {
  setBibleStageReviewWaiting(stage, false)
  if (stage === 'worldbuilding') bibleError.value = error
  else if (stage === 'characters') charactersError.value = error
  else if (stage === 'locations') locationsError.value = error
}

async function markBibleStageCommitted(stage: string, isCurrent = () => true) {
  const loaded = await loadBibleData(stage === 'worldbuilding', isCurrent)
  if (!isCurrent()) return false
  if (!loaded) {
    failBibleStage(stage, '生成结果已保存，但读取失败，请检查连接后重试')
    return false
  }
  if (stage === 'worldbuilding') {
    completedDimensions.value = new Set(WB_DIMS)
    generatingBible.value = false
    bibleGenerated.value = true
  } else if (stage === 'characters') {
    generatingCharacters.value = false
    charactersGenerated.value = true
  } else if (stage === 'locations') {
    generatingLocations.value = false
    locationsGenerated.value = true
  }
  phaseMessage.value = ''
  return true
}

async function openBibleReviewPanel(stage: 'worldbuilding' | 'characters' | 'locations', sessionId: string) {
  if (!sessionId) return
  const previousSessionId = bibleStageSessions.get(stage)
  if (previousSessionId) {
    bibleInvocationUnsubs.get(previousSessionId)?.()
    bibleInvocationUnsubs.delete(previousSessionId)
  }
  bibleStageSessions.set(stage, sessionId)
  if (featureFlags.aiInvocationDebug) {
    setBibleStageReviewWaiting(stage, true)
  } else {
    setBibleStageHeadlessGenerating(stage)
  }
  try {
    bibleInvocationUnsubs.get(sessionId)?.()
    const unsub = aiInvocationStore.onSessionUpdate(sessionId, (payload) => {
      const status = String(payload.session?.status || '')
      if (featureFlags.aiInvocationDebug && status === 'awaiting_pre_call_review') {
        setBibleStageReviewWaiting(stage, true)
      } else {
        // Reset the generic busy label first; preview parsing below may replace
        // it with an item count from the live attempt content.
        setBibleStageHeadlessGenerating(stage)
      }
      applyBibleInvocationPreview(stage, payload)
      if (featureFlags.aiInvocationDebug && status === 'awaiting_acceptance') {
        phaseMessage.value = '地点草稿已生成，等待审阅确认...'
      } else if (featureFlags.aiInvocationDebug && ['awaiting_commit', 'committing'].includes(status)) {
        phaseMessage.value = '审阅已完成，正在校验并保存地点...'
      }
      const failure = invocationFailureMessage(payload)
      if (failure) {
        failBibleStage(stage, failure)
        return
      } else if (payload.session?.status === 'completed' || payload.commit?.status === 'succeeded') {
        void markBibleStageCommitted(stage)
      } else {
        if (stage === 'worldbuilding') bibleError.value = ''
        else if (stage === 'characters') charactersError.value = ''
        else locationsError.value = ''
        return
      }
      bibleInvocationUnsubs.get(sessionId)?.()
      bibleInvocationUnsubs.delete(sessionId)
    }, (error) => {
      failBibleStage(stage, error)
    })
    bibleInvocationUnsubs.set(sessionId, unsub)
    await aiInvocationStore.open(sessionId)
    if (aiInvocationStore.session?.id === sessionId) {
      applyBibleInvocationPreview(stage, {
        session: aiInvocationStore.session,
        attempt: aiInvocationStore.attempt,
        decision: aiInvocationStore.decision,
        commit: aiInvocationStore.commit,
        next_action: aiInvocationStore.nextAction,
      })
    }
  } catch (e: unknown) {
    bibleInvocationUnsubs.get(sessionId)?.()
    bibleInvocationUnsubs.delete(sessionId)
    failBibleStage(stage, formatApiError(e) || 'AI 调用处理失败')
  }
}

// ── Step 4：剧情总纲 ──
const plotOutline = ref<PlotOutlineDTO | null>(null)
const plotOutlineGenerating = ref(false)
const plotOutlineError = ref('')
const plotOutlineCommitted = ref(false)
const plotOutlineSessionId = ref('')
const step4RestoredFromCache = ref(false)
const editablePlotOutline = ref<PlotOutlineDTO>(createEmptyPlotOutline())
const syncingPlotOutlineDraft = ref(false)
const plotOutlineStatus = ref<PlotOutlineStatus>('idle')
const plotOutlineTopFieldKeys = computed(() => {
  return getPlotOutlineTopFieldKeys(editablePlotOutline.value)
})
const plotOutlineTotalChapters = computed(() => {
  const maxStageEnd = Math.max(
    0,
    ...editablePlotOutline.value.stage_plan.map(stage =>
      typeof stage.chapter_end === 'number' ? stage.chapter_end : 0
    ),
  )
  return Math.max(1, props.targetChapters || 0, maxStageEnd)
})
const plotOutlineBusy = computed(() =>
  plotOutlineGenerating.value ||
  (plotOutlineStatus.value !== 'idle' && plotOutlineStatus.value !== 'done' && plotOutlineStatus.value !== 'error')
)
const isWizardGenerating = computed(() =>
  generatingBible.value || generatingCharacters.value || generatingLocations.value || plotOutlineBusy.value || setupDag.busy.value
)
const plotOutlineStatusMessage = computed(() => {
  if (phaseMessage.value) return phaseMessage.value
  if (plotOutlineStatus.value === 'creating') return '正在创建剧情总纲任务...'
  if (plotOutlineStatus.value === 'reviewing') {
    return featureFlags.aiInvocationDebug ? '等待 AI 审阅确认...' : '正在确认剧情总纲生成...'
  }
  if (plotOutlineStatus.value === 'generating') return 'AI 正在生成剧情总纲...'
  if (plotOutlineStatus.value === 'committing') return '正在写入剧情总纲...'
  return '正在生成剧情总纲...'
})
const plotOutlineLivePreview = computed(() => {
  if (!plotOutlineSessionId.value) return ''
  if (aiInvocationStore.session?.id !== plotOutlineSessionId.value) return ''
  const text = aiInvocationStore.liveAttemptDisplay.trim()
  if (!text) return ''
  return text.length > 1000 ? text.slice(-1000) : text
})
const plotOutlineProgressItems = computed<PlotOutlineProgressItem[]>(() => {
  const run = setupRun.value
  if (!run || run.stage !== 'plot_outline') return []
  return run.graph.nodes.map(node => {
    const status = run.node_states[node.id]?.status || 'pending'
    return { key: node.id, label: node.label, desc: run.progress.current_node_id === node.id ? run.progress.message : '',
      state: status === 'success' || status === 'bypassed' ? 'done' : status === 'running' ? 'active' : 'pending' }
  })
})

function syncEditablePlotOutline(outline: PlotOutlineDTO | null | undefined) {
  syncingPlotOutlineDraft.value = true
  editablePlotOutline.value = clonePlotOutline(outline, plotOutlineTotalChapters.value)
  queueMicrotask(() => {
    syncingPlotOutlineDraft.value = false
  })
}

function normalizeIncomingPlotOutline(outline: PlotOutlineDTO | null | undefined): PlotOutlineDTO | null {
  return normalizePlotOutlineShape(outline, plotOutlineTotalChapters.value)
}

function updateStageChapterNumber(
  index: number,
  key: 'chapter_start' | 'chapter_end',
  value: number | null,
) {
  const stage = editablePlotOutline.value.stage_plan[index]
  if (!stage) return
  stage[key] = typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function stageRangePercentLabel(stage: { chapter_start?: number; chapter_end?: number; range_percent?: string }): string {
  return buildStageRangePercentLabel(stage, plotOutlineTotalChapters.value)
}

function buildEditablePlotOutlinePayload(): PlotOutlineDTO {
  return buildPlotOutlinePayload(editablePlotOutline.value, plotOutlineTotalChapters.value)
}

function touchPlotOutlineDraft() {
  if (syncingPlotOutlineDraft.value) return
  if (!plotOutline.value) return
  plotOutline.value = buildEditablePlotOutlinePayload()
  plotOutlineCommitted.value = false
}

function persistStepFourUiToCache(opts?: { includePlotOutline?: boolean }) {
  if (currentStep.value !== 4) return
  const patch: Partial<Omit<WizardUiCachePayload, 'v' | 'novelId'>> = {
    invocationSessionId: plotOutlineSessionId.value || undefined,
  }
  if (opts?.includePlotOutline) {
    patch.plotOutline = plotOutline.value || undefined
  }
  writeWizardUiCache(props.novelId, patch)
}

function finishPlotOutlineInvocation() {
  plotOutlineGenerating.value = false
  plotOutlineStatus.value = 'done'
  phaseMessage.value = ''
  mainPlotSessionUnsub?.()
  mainPlotSessionUnsub = null
}

function failPlotOutlineInvocation(messageText: string) {
  plotOutlineError.value = messageText
  plotOutlineGenerating.value = false
  plotOutlineStatus.value = 'error'
  phaseMessage.value = ''
}

function resetPlotOutlineInvocationState() {
  plotOutlineGenerating.value = false
  plotOutlineStatus.value = 'idle'
  phaseMessage.value = ''
}

function updatePlotOutlineStatusFromInvocation(payload: InvocationResponseDTO) {
  const commitStatus = String(payload.commit?.status || '')
  const sessionStatus = String(payload.session?.status || '')
  if (commitStatus === 'succeeded' || sessionStatus === 'completed') {
    plotOutlineStatus.value = 'committing'
    phaseMessage.value = '正在写入剧情总纲...'
    return
  }
  if (commitStatus === 'failed' || sessionStatus === 'failed' || sessionStatus === 'cancelled' || sessionStatus === 'blocked') {
    return
  }
  if (sessionStatus === 'awaiting_commit' || sessionStatus === 'committing' || commitStatus === 'running') {
    plotOutlineStatus.value = 'committing'
    phaseMessage.value = '正在写入剧情总纲...'
    return
  }
  if (sessionStatus === 'generating') {
    plotOutlineStatus.value = 'generating'
    phaseMessage.value = 'AI 正在生成剧情总纲...'
    return
  }
  if (sessionStatus === 'awaiting_acceptance') {
    plotOutlineStatus.value = featureFlags.aiInvocationDebug ? 'reviewing' : 'generating'
    phaseMessage.value = featureFlags.aiInvocationDebug ? '等待 AI 审阅确认...' : '正在确认剧情总纲生成...'
    return
  }
  if (sessionStatus === 'awaiting_pre_call_review') {
    plotOutlineStatus.value = featureFlags.aiInvocationDebug ? 'reviewing' : 'creating'
    phaseMessage.value = featureFlags.aiInvocationDebug ? '等待 AI 审阅确认...' : '正在准备剧情总纲生成...'
    return
  }
  plotOutlineStatus.value = 'creating'
  phaseMessage.value = '正在创建剧情总纲任务...'
}

async function refreshPlotOutlineFromApi(isCurrent = () => true): Promise<boolean> {
  try {
    const response = await workflowApi.getPlotOutline(props.novelId)
    if (!isCurrent()) return false
    if (!response.plot_outline) return false
    const normalized = normalizeIncomingPlotOutline(response.plot_outline)
    if (!normalized) return false
    plotOutline.value = normalized
    syncEditablePlotOutline(normalized)
    plotOutlineCommitted.value = true
    writeWizardUiCache(props.novelId, { plotOutline: normalized })
    return true
  } catch {
    return false
  }
}

function applyPlotOutlineFromResult(
  result: Record<string, unknown>,
  outputBindings: InvocationVariableBinding[] = [],
): boolean {
  const outline = extractPlotOutlineFromResult(result, outputBindings, plotOutlineTotalChapters.value)
  if (!outline) return false
  plotOutline.value = outline
  syncEditablePlotOutline(outline)
  plotOutlineCommitted.value = true
  writeWizardUiCache(props.novelId, { plotOutline: outline })
  message.success('AI 审阅已完成，剧情总纲已回填')
  finishPlotOutlineInvocation()
  return true
}

async function handlePlotOutlineInvocationUpdate(payload: InvocationResponseDTO) {
  updatePlotOutlineStatusFromInvocation(payload)
  const failure = invocationFailureMessage(payload)
  if (failure) {
    failPlotOutlineInvocation(failure)
    return
  }
  plotOutlineError.value = ''
  plotOutlineGenerating.value = true

  if (payload.commit?.status === 'succeeded' || payload.session.status === 'completed') {
    const result = payload.commit?.result
    if (result && applyPlotOutlineFromResult(result, payload.session.output_bindings || [])) return
    const refreshed = await refreshPlotOutlineFromApi()
    if (refreshed) {
      message.success('AI 审阅已完成，剧情总纲已回填')
      finishPlotOutlineInvocation()
    } else {
      failPlotOutlineInvocation('剧情总纲生成完成，但未能读取结果，请重试')
    }
  }
}

async function openPlotOutlineReviewPanel(sessionId: string) {
  if (!sessionId) return
  plotOutlineSessionId.value = sessionId
  plotOutlineGenerating.value = true
  if (plotOutlineStatus.value === 'idle' || plotOutlineStatus.value === 'done' || plotOutlineStatus.value === 'error') {
    plotOutlineStatus.value = 'creating'
    phaseMessage.value = '正在创建剧情总纲任务...'
  }
  if (featureFlags.aiInvocationDebug) {
    message.info('已进入 AI 审阅')
  }
  try {
    writeWizardUiCache(props.novelId, { invocationSessionId: sessionId })
    mainPlotSessionUnsub?.()
    mainPlotSessionUnsub = aiInvocationStore.onSessionUpdate(sessionId, (payload) => {
      void handlePlotOutlineInvocationUpdate(payload)
    }, failPlotOutlineInvocation)
    await aiInvocationStore.open(sessionId)
    if (aiInvocationStore.session?.id === sessionId) {
      await handlePlotOutlineInvocationUpdate({
        session: aiInvocationStore.session,
        attempt: aiInvocationStore.attempt,
        decision: aiInvocationStore.decision,
        commit: aiInvocationStore.commit,
        next_action: aiInvocationStore.nextAction,
      })
    }
  } catch (e: unknown) {
    failPlotOutlineInvocation(formatApiError(e) || 'AI 调用处理失败')
  }
}

async function loadPlotOutline(opts?: { forceNew?: boolean }) {
  if (setupDag.busy.value || setupDag.loading.value) return
  step4RestoredFromCache.value = false
  plotOutlineError.value = ''
  if (!opts?.forceNew && !isStageLocked('plot_outline') && await refreshPlotOutlineFromApi()) {
    finishPlotOutlineInvocation()
    return
  }
  plotOutlineCommitted.value = false
  plotOutlineSessionId.value = ''
  await startSetupStage('plot_outline')
}

async function refreshPlotOutline() {
  restartSetupStage('plot_outline')
}

function hydrateStepFourFromCache() {
  const cached = readWizardUiCache(props.novelId)
  if (!cached || !isPlotOutlineCacheFresh(cached) || !cached.plotOutline) return
  const normalized = normalizeIncomingPlotOutline(cached.plotOutline)
  plotOutline.value = normalized
  syncEditablePlotOutline(normalized)
  step4RestoredFromCache.value = true
}

const unverifiedStages = ref(new Set<SetupStageId>())
const verifiedPreviewKeys = new Map<string, string>()
let previewRevision = 0
const setupDag = useSetupDag(() => props.novelId, {
  onUpdate(run, isCurrent) {
    if ((run.workflow_version ?? 1) >= 2) unverifiedStages.value.add(run.stage)
    phaseMessage.value = run.progress.current_node_label || run.progress.message
    if (run.stage === 'plot_outline') {
      plotOutlineGenerating.value = run.status === 'running' || run.status === 'completed'
      plotOutlineStatus.value = run.status === 'running' ? 'generating' : run.status === 'completed' ? 'committing' : 'error'
      plotOutlineCommitted.value = false
    } else {
      const busy = run.status === 'running' || run.status === 'completed'
      if (run.stage === 'worldbuilding') { generatingBible.value = busy; bibleGenerated.value = false }
      else if (run.stage === 'characters') { generatingCharacters.value = busy; charactersGenerated.value = false }
      else { generatingLocations.value = busy; locationsGenerated.value = false }
    }
    const verified = run.graph.nodes.filter(node => {
      const vars = node.config.prompt_variables as { action?: string; unit?: string } | undefined
      return vars?.action === 'verify' && ['success', 'completed'].includes(run.node_states[node.id]?.status || '')
    })
    if (run.stage === 'worldbuilding') {
      completedDimensions.value = new Set(verified.map(node => (node.config.prompt_variables as { unit: string }).unit).filter((unit): unit is WorldbuildingDimKey => WB_DIMS.includes(unit as WorldbuildingDimKey)))
      const active = run.graph.nodes.find(node => node.id === run.progress.current_node_id)
      const unit = (active?.config.prompt_variables as { unit?: string } | undefined)?.unit
      activeDimension.value = WB_DIMS.includes(unit as WorldbuildingDimKey) ? unit as WorldbuildingDimKey : ''
    }
    const key = verified.map(node => node.id).join('|')
    if (run.status !== 'completed' && key && verifiedPreviewKeys.get(run.run_id) !== key) {
      verifiedPreviewKeys.set(run.run_id, key)
      const revision = ++previewRevision
      const valid = () => isCurrent() && revision === previewRevision && setupRun.value?.status !== 'completed'
      if (run.stage === 'plot_outline') void refreshPlotOutlineFromApi(valid)
      else void loadBibleData(false, valid)
    }
  },
  async onCompleted(run, isCurrent) {
    previewRevision++
    if (run.stage === 'plot_outline') {
      const loaded = await refreshPlotOutlineFromApi(isCurrent)
      if (!isCurrent()) return false
      if (!loaded) {
        failPlotOutlineInvocation('生成结果已保存，但读取失败，请检查连接后重新连接。')
        return false
      }
      finishPlotOutlineInvocation()
      unverifiedStages.value.delete(run.stage)
      return true
    }
    const loaded = await markBibleStageCommitted(run.stage, isCurrent)
    if (loaded && isCurrent()) unverifiedStages.value.delete(run.stage)
    return loaded
  },
  onFailure(stage, error) {
    unverifiedStages.value.add(stage)
    if (stage === 'plot_outline') failPlotOutlineInvocation(error)
    else failBibleStage(stage, error)
  },
})
const { stages: setupStages, run: setupRun, busy: setupBusy, loading: setupLoading, stopping: setupStopping, error: setupError } = setupDag
const openingWizard = ref(false)
const stepStages: SetupStageId[] = ['worldbuilding', 'characters', 'locations', 'plot_outline']
const stageNames: Record<SetupStageId, string> = { worldbuilding: '文风 / 世界观', characters: '人物', locations: '地图', plot_outline: '剧情总纲' }
function stageRun(stage: SetupStageId) { return setupStages.value.find(item => item.id === stage)?.latest_run || (setupRun.value?.stage === stage ? setupRun.value : null) }
function isStageIncomplete(stage: SetupStageId) { const run = stageRun(stage); return unverifiedStages.value.has(stage) || Boolean(run && (run.workflow_version ?? 1) >= 2 && run.status !== 'completed') }
function stageDependencyLock(stage: SetupStageId) { return stepStages.slice(stepStages.indexOf(stage) + 1).find(isStageIncomplete) || null }
function isStageLocked(stage: SetupStageId) { return isStageIncomplete(stage) || Boolean(stageDependencyLock(stage)) }
const currentStageIncomplete = computed(() => Boolean(stepStages[currentStep.value - 1] && isStageIncomplete(stepStages[currentStep.value - 1]!)))
const currentDependencyLock = computed(() => stepStages[currentStep.value - 1] ? stageDependencyLock(stepStages[currentStep.value - 1]!) : null)
const currentStageLocked = computed(() => openingWizard.value || setupLoading.value || Boolean(stepStages[currentStep.value - 1] && isStageLocked(stepStages[currentStep.value - 1]!)))
function stageStartLabel(stage: SetupStageId) { return isStageIncomplete(stage) ? '继续生成' : '开始生成' }
const savedStagePreview = computed(() => {
  if (currentStep.value === 1) return [
    ...(styleText.value ? [{ label: '文风公约', content: styleText.value }] : []),
    ...wbDimensionCards.value.filter(dim => orderedWorldbuildingFields(dim.key).length).map(dim => ({ label: dim.label, content: orderedWorldbuildingFields(dim.key).map(field => `${worldbuildingFieldTitle(dim.key, field.key)}：${field.value}`).join('\n') })),
  ]
  if (currentStep.value === 2) return editableCharacters.value.map(char => ({ label: char.name, content: char.background || char.description || '' }))
  if (currentStep.value === 3) return editableLocations.value.map(loc => ({ label: loc.name, content: loc.description }))
  if (currentStep.value === 4 && plotOutline.value) return [{ label: '剧情总纲', content: plotFieldText(plotOutline.value, 'main_story_overview') }]
  return []
})

async function startSetupStage(stage: SetupStageId, restart = false) {
  if (setupDag.busy.value || setupDag.loading.value || stageDependencyLock(stage)) return
  unverifiedStages.value.add(stage)
  previewRevision++
  if (stage === 'plot_outline') {
    plotOutlineError.value = ''
    plotOutlineGenerating.value = true
    plotOutlineStatus.value = 'creating'
    plotOutlineCommitted.value = false
  } else {
    if (stage === 'worldbuilding') bibleError.value = ''
    else if (stage === 'characters') charactersError.value = ''
    else locationsError.value = ''
    setBibleStageHeadlessGenerating(stage)
  }
  phaseMessage.value = '正在创建生成任务…'
  await setupDag.start(stage, restart)
}
function restartSetupStage(stage: SetupStageId) {
  if (isWizardGenerating.value || setupLoading.value || stageDependencyLock(stage)) return
  dialog.warning({ title: '从头重新生成', content: '这会重新执行本阶段全部生成与审核步骤，不沿用已成功的检查点。已有资料会在新结果通过审核后更新。', positiveText: '从头重新生成', negativeText: '取消', onPositiveClick: () => startSetupStage(stage, true) })
}
function startBibleGeneration() { return startSetupStage('worldbuilding') }
function startCharactersGeneration() { return startSetupStage('characters') }
function startLocationsGeneration() { return startSetupStage('locations') }

/** 生成完成后读取已保存的设定，世界观读取失败时不标记生成成功。 */
async function loadBibleData(requireWorldbuilding = false, isCurrent = () => true) {
  try {
    const bible = await bibleApi.getBible(props.novelId)
    if (!isCurrent()) return false

    let fromApi = emptyWorldbuildingShape()
    try {
      const w = await worldbuildingApi.getWorldbuilding(props.novelId)
      fromApi = normalizeWorldbuildingFromApi(w as unknown as Record<string, unknown>)
    } catch {
      if (requireWorldbuilding && !hasWorldbuildingContent(worldbuildingFromWorldSettings(bible.world_settings))) return false
    }
    if (!isCurrent()) return false
    bibleData.value = bible
    const fromWs = worldbuildingFromWorldSettings(bible.world_settings)
    worldbuildingData.value = mergeWorldbuildingDisplay(fromApi, fromWs)

    // 始终用后端最新数据刷新文风
    styleText.value = styleConventionFromBible(bible)

    // 将人物/地点拷贝到可编辑列表
    editableCharacters.value = (bible.characters || []).map((char) =>
      mapCharacterToEditable(char, generatedCharacterDrafts.value[characterDraftKey(char)])
    )
    editableLocations.value = (bible.locations || []).map(l => ({
      name: l.name || '',
      id: l.id || undefined,
      parent_id: l.parent_id ?? null,
      location_type: l.location_type || '',
      description: l.description || '',
    }))
    return true
  } catch (error) {
    console.error('Failed to load Bible data:', error)
    return false
  }
}

// ════════════════════════════════════════════════════════════════════════════
// 向导生命周期
// ════════════════════════════════════════════════════════════════════════════

function resetWizardStateForOpen() {
  previewRevision++
  verifiedPreviewKeys.clear()
  generatingBible.value = false
  generatingCharacters.value = false
  generatingLocations.value = false
  bibleGenerated.value = false
  charactersGenerated.value = false
  locationsGenerated.value = false
  bibleError.value = ''
  plotOutlineGenerating.value = false
  plotOutlineStatus.value = 'idle'
  currentStep.value = 1
  stepStatus.value = 'process'
  plotOutline.value = null
  syncEditablePlotOutline(null)
  plotOutlineCommitted.value = false
  plotOutlineSessionId.value = ''
  plotOutlineError.value = ''
  charactersError.value = ''
  locationsError.value = ''
  resumedFromStep.value = 0
  streamingCharacters.value = []
  streamingLocations.value = []
  editableCharacters.value = []
  editableLocations.value = []
}

async function detectWizardProgress(): Promise<number> {
  try {
    const bible = await bibleApi.getBible(props.novelId)
    bibleData.value = bible

    let fromApi = emptyWorldbuildingShape()
    try {
      const w = await worldbuildingApi.getWorldbuilding(props.novelId)
      fromApi = normalizeWorldbuildingFromApi(w as unknown as Record<string, unknown>)
    } catch { /* 404 */ }
    const fromWs = worldbuildingFromWorldSettings(bible.world_settings)
    worldbuildingData.value = mergeWorldbuildingDisplay(fromApi, fromWs)
    styleText.value = styleConventionFromBible(bible)

    // ── 判断后端是否已有数据（用于决定步骤内部显示"生成中"还是"可编辑预览"） ──
    const hasWorldbuilding = hasWorldbuildingContent(fromWs) || hasWorldbuildingContent(worldbuildingData.value)
    const hasStyle = styleConventionFromBible(bible).length > 0
    const hasCharacters = (bible.characters?.length ?? 0) > 0
    const hasLocations = (bible.locations?.length ?? 0) > 0

    // 有数据就标记为"已生成"（步骤内展示可编辑预览），没有则展示"生成中"或初始状态
    if (hasWorldbuilding || hasStyle) {
      bibleGenerated.value = true
    }
    if (hasCharacters) {
      charactersGenerated.value = true
      editableCharacters.value = (bible.characters || []).map((char) =>
        mapCharacterToEditable(char, generatedCharacterDrafts.value[characterDraftKey(char)])
      )
    }
    if (hasLocations) {
      locationsGenerated.value = true
      editableLocations.value = (bible.locations || []).map(l => ({
        name: l.name || '',
        id: l.id || undefined,
        parent_id: l.parent_id ?? null,
        location_type: l.location_type || '',
        description: l.description || '',
      }))
    }

    // ── 判断剧情总纲是否已提交 ──
    let hasPlotOutline = false
    try {
      const response = await workflowApi.getPlotOutline(props.novelId)
      if (response.plot_outline) {
        const normalized = normalizeIncomingPlotOutline(response.plot_outline)
        if (normalized) {
          plotOutline.value = normalized
          syncEditablePlotOutline(normalized)
          plotOutlineCommitted.value = true
          hasPlotOutline = true
        }
      }
    } catch { /* 忽略 */ }

    // ── 决定恢复到哪一步：优先用缓存的 lastStep，没缓存才按后端数据推断 ──
    const cached = readWizardUiCache(props.novelId)
    const cachedLastStep = cached?.lastStep

    if (cachedLastStep && cachedLastStep >= 1 && !cached?.wizardCompleted) {
      // 有缓存且未完成 → 回到上次停下的步骤（不跳过）
      resumedFromStep.value = cachedLastStep
      return cachedLastStep
    }

    // 没有缓存时，回到最近一个已生成但尚未确认的步骤。
    // 生成完成只展示可编辑预览，只有用户点"下一步"才进入下一阶段。
    if (!hasWorldbuilding && !hasStyle) {
      resumedFromStep.value = 0
      return 1
    }
    if (!hasCharacters) {
      resumedFromStep.value = 1
      return 1
    }
    if (!hasLocations) {
      resumedFromStep.value = 2
      return 2
    }
    if (!hasPlotOutline) {
      resumedFromStep.value = 3
      return 3
    }

    resumedFromStep.value = 5
    return 5
  } catch (err) {
    console.warn('[NovelSetupGuide] detectWizardProgress failed:', err)
    return 1
  }
}

async function runWizardOpenSequence() {
  openingWizard.value = true
  try {
    resetWizardStateForOpen()
    const step = await detectWizardProgress()
    if (!props.show) return
    await setupDag.resume()
    if (!props.show) return
    const active = setupRun.value?.status === 'running' ? setupRun.value : null
    const unfinished = setupStages.value.find(stage => isStageIncomplete(stage.id))
    currentStep.value = active ? stepStages.indexOf(active.stage) + 1 : unfinished ? stepStages.indexOf(unfinished.id) + 1 : step
    maxVisitedStep.value = Math.max(step, currentStep.value)
    if (currentStep.value === 4 && !plotOutlineCommitted.value && !active) hydrateStepFourFromCache()
  } finally {
    openingWizard.value = false
  }
  if (props.show && currentStep.value === 4 && !plotOutline.value && !setupDag.busy.value && !setupError.value) {
    void loadPlotOutline()
  }
}

function stopGenerationOnClose() {
  previewRevision++
  setupDag.pause()
  generatingBible.value = false
  generatingCharacters.value = false
  generatingLocations.value = false
  plotOutlineGenerating.value = false
  plotOutlineStatus.value = 'idle'
  phaseMessage.value = ''
  mainPlotSessionUnsub?.()
  mainPlotSessionUnsub = null
  for (const unsub of bibleInvocationUnsubs.values()) unsub()
  bibleInvocationUnsubs.clear()
  bibleStageSessions.clear()
}

watch(
  () => props.show,
  async (val) => {
    if (val) {
      await runWizardOpenSequence()
    } else {
      stopGenerationOnClose()
      persistStepFourUiToCache({ includePlotOutline: true })
    }
  }
)

onMounted(async () => {
  if (props.show) {
    await runWizardOpenSequence()
  }
})

onUnmounted(() => {
  stopGenerationOnClose()
})

watch(currentStep, (step, prevStep) => {
  // 记录向导进度到缓存
  if (props.show) {
    setWizardLastStep(props.novelId, step)
  }
  // 切换步骤时刷新数据（排除初次加载，首次由 runWizardOpenSequence 处理）
  if (prevStep !== undefined && props.show && !openingWizard.value && !setupDag.busy.value) {
    void loadBibleData()
  }
  if (step === 4 && props.show && !openingWizard.value && !setupDag.busy.value && !isStageLocked('plot_outline') && !plotOutlineCommitted.value && !plotOutline.value && !plotOutlineGenerating.value) {
    void loadPlotOutline()
  }
})

watch(plotOutline, () => {
  if (currentStep.value === 4 && props.show) persistStepFourUiToCache()
}, { deep: true })

watch(editablePlotOutline, () => {
  if (currentStep.value === 4 && props.show) touchPlotOutlineDraft()
}, { deep: true })

/** 保存中状态 */
const savingStep = ref(false)

/** 保存步骤1的编辑（世界观 + 文风）到后端 */
async function saveWorldbuildingEdits(): Promise<boolean> {
  if (isStageLocked('worldbuilding') || isWizardGenerating.value || setupLoading.value) return false
  try {
    // 保存世界观维度数据
    const wbData: Record<string, Record<string, string>> = {}
    for (const dim of WB_DIMS) {
      wbData[dim] = { ...worldbuildingData.value[dim] }
    }
    await worldbuildingApi.updateWorldbuilding(props.novelId, wbData as any)

    // 保存文风公约。世界观主数据已写入 Worldbuilding V2；Bible.world_settings
    // 只保留用户/系统补充的零散规则，不再承载五维世界观。
    const existing = await bibleApi.getBible(props.novelId)
    if (styleText.value) {
      await bibleApi.updateBible(props.novelId, {
        characters: existing.characters || [],
        world_settings: existing.world_settings || [],
        locations: existing.locations || [],
        timeline_notes: existing.timeline_notes || [],
        style_notes: [{
          id: `${props.novelId}-style-1`,
          category: '文风公约',
          content: styleText.value,
        }],
      })
    } else {
      await bibleApi.updateBible(props.novelId, {
        characters: existing.characters || [],
        world_settings: existing.world_settings || [],
        locations: existing.locations || [],
        timeline_notes: existing.timeline_notes || [],
        style_notes: existing.style_notes || [],
      })
    }
    return true
  } catch (e) {
    message.error(formatApiError(e) || '保存世界观修改失败')
    return false
  }
}

/** 保存步骤2的编辑（人物）到后端 */
async function saveCharactersEdits(): Promise<boolean> {
  if (isStageLocked('characters') || isWizardGenerating.value || setupLoading.value) return false
  try {
    const existing = await bibleApi.getBible(props.novelId)
    await bibleApi.updateBible(props.novelId, {
      characters: editableCharacters.value.map((c, idx) => ({
        id: c.id || `${props.novelId}-char-${idx + 1}`,
        name: c.name,
        description: formatCharacterDescriptionForSave(c.role, c.description),
        role: c.role,
        gender: c.gender,
        age: c.age,
        appearance: c.appearance,
        personality: c.personality,
        background: c.background,
        core_motivation: c.core_motivation,
        inner_lack: c.inner_lack,
        mental_state: c.mental_state,
        mental_state_reason: c.mental_state_reason,
        verbal_tic: c.verbal_tic,
        idle_behavior: c.idle_behavior,
        relationships: serializeRelationships(c.relationships || []),
        public_profile: c.public_profile,
        hidden_profile: c.hidden_profile,
        reveal_chapter: c.reveal_chapter,
        core_belief: c.core_belief,
        moral_taboos: c.moral_taboos,
        voice_profile: c.voice_profile,
        active_wounds: c.active_wounds,
      })),
      world_settings: existing.world_settings || [],
      locations: existing.locations || [],
      timeline_notes: existing.timeline_notes || [],
      style_notes: existing.style_notes || [],
    })
    return true
  } catch (e) {
    message.error(formatApiError(e) || '保存人物修改失败')
    return false
  }
}

const bulkExtractingPsyche = ref(false)

async function runBulkCharacterExtract() {
  const list = editableCharacters.value.filter((c) => c.name.trim())
  if (!list.length) {
    message.warning('请先填写人物姓名')
    return
  }
  bulkExtractingPsyche.value = true
  try {
    const res = await characterPsycheApi.autofill(props.novelId, { mode: 'all' })
    const failed = res.characters.filter((c) => !c.ok)
    await loadBibleData()
    if (failed.length) {
      message.warning(
        `${failed.length} 位失败：` + failed.map((f) => `${f.name}（${(f.error || '').slice(0, 80)}）`).slice(0, 4).join('；'),
      )
    } else {
      message.success(
        `已从简介同步空锚点（启发式，无模型），共 ${res.characters.length} 位角色；请在预览中核对后保存`,
      )
    }
  } catch (e: unknown) {
    message.error(formatApiError(e) || '同步失败')
  } finally {
    bulkExtractingPsyche.value = false
  }
}

/** 保存步骤3的编辑（地点）到后端 */
async function saveLocationsEdits(): Promise<boolean> {
  if (isStageLocked('locations') || isWizardGenerating.value || setupLoading.value) return false
  try {
    const existing = await bibleApi.getBible(props.novelId)
    const locationIds = new Set(editableLocations.value.map(l => l.id).filter(Boolean))
    await bibleApi.updateBible(props.novelId, {
      characters: existing.characters || [],
      world_settings: existing.world_settings || [],
      locations: editableLocations.value.map(l => ({
        id: l.id || '',
        name: l.name,
        description: l.description,
        location_type: l.location_type || '场景',
        // Deleting a parent in the editor promotes its remaining children to roots.
        parent_id: l.parent_id && locationIds.has(l.parent_id) ? l.parent_id : null,
      })),
      timeline_notes: existing.timeline_notes || [],
      style_notes: existing.style_notes || [],
    })
    return true
  } catch (e) {
    message.error(formatApiError(e) || '保存地点修改失败')
    return false
  }
}

async function savePlotOutlineEdits(): Promise<boolean> {
  if (isStageLocked('plot_outline') || isWizardGenerating.value || setupLoading.value) return false
  try {
    const payload = buildEditablePlotOutlinePayload()
    const validationError = validateEditablePlotOutline(payload)
    if (validationError) {
      message.error(validationError)
      return false
    }
    const response = await workflowApi.savePlotOutline(props.novelId, payload)
    const saved = response.plot_outline || payload
    plotOutline.value = saved
    syncEditablePlotOutline(saved)
    plotOutlineCommitted.value = true
    writeWizardUiCache(props.novelId, { plotOutline: saved })
    return true
  } catch (e) {
    message.error(formatApiError(e) || '保存剧情总纲失败')
    return false
  }
}

/** 步骤最大可达步骤（用户走过的最远步骤） */
const maxVisitedStep = ref(1)

/** 点击步骤导航条切换步骤（只允许切换到已到过的步骤） */
function goToStep(step: number) {
  if (step < 1 || step > 5) return
  if (step > maxVisitedStep.value) return // 不允许跳到还没到过的步骤
  if (step === currentStep.value) return
  // 正在生成中不允许切换
  if (isWizardGenerating.value) return
  if (setupLoading.value || (step > currentStep.value && stepStages.slice(0, step - 1).some(isStageIncomplete))) return
  currentStep.value = step
}

/** 上一步 */
function handlePrev() {
  if (currentStep.value > 1) {
    // 正在生成中不允许返回
    if (isWizardGenerating.value) return
    currentStep.value--
  }
}

const handleNext = async () => {
  if (savingStep.value || currentStageLocked.value || isWizardGenerating.value) return
  if ((currentStep.value === 1 && !bibleGenerated.value) || (currentStep.value === 2 && !charactersGenerated.value) || (currentStep.value === 3 && !locationsGenerated.value) || (currentStep.value === 4 && !plotOutline.value)) return
  savingStep.value = true
  try {
    if (currentStep.value === 1) {
      // 先保存用户对世界观的编辑
      const ok = await saveWorldbuildingEdits()
      if (!ok) return
      currentStep.value = 2
      maxVisitedStep.value = Math.max(maxVisitedStep.value, 2)
      if (charactersGenerated.value) return
      startCharactersGeneration()
    } else if (currentStep.value === 2) {
      // 先保存用户对人物的编辑
      const ok = await saveCharactersEdits()
      if (!ok) return
      currentStep.value = 3
      maxVisitedStep.value = Math.max(maxVisitedStep.value, 3)
      if (locationsGenerated.value) return
      startLocationsGeneration()
    } else if (currentStep.value === 3) {
      // 先保存用户对地点的编辑
      const ok = await saveLocationsEdits()
      if (!ok) return
      currentStep.value = 4
      maxVisitedStep.value = Math.max(maxVisitedStep.value, 4)
    } else if (currentStep.value === 4) {
      const ok = await savePlotOutlineEdits()
      if (!ok) return
      currentStep.value = 5
      maxVisitedStep.value = Math.max(maxVisitedStep.value, 5)
    } else if (currentStep.value < 5) {
      currentStep.value++
      maxVisitedStep.value = Math.max(maxVisitedStep.value, currentStep.value)
    }
  } finally {
    savingStep.value = false
  }
}

const dialog = useDialog()

const handleSkip = () => {
  if (isWizardGenerating.value || setupLoading.value || stepStages.some(isStageLocked)) return
  dialog.warning({
    title: '确认跳过向导',
    content: '已写入作品的数据会保留；第 4 步未提交的剧情总纲预览仍会缓存在本机，便于以后从向导继续。',
    positiveText: '跳过',
    negativeText: '取消',
    onPositiveClick: () => {
      markWizardCompleted(props.novelId)
      emit('skip')
      emit('update:show', false)
    },
  })
}

const requestClose = () => {
  dialog.warning({
    title: '关闭向导',
    content: isWizardGenerating.value ? '关闭后生成会在后台继续。重新打开向导可恢复进度；如需取消，请先点击「停止生成」。' : '进度已按步骤写入作品；第 4 步未提交的剧情总纲预览会缓存在本机以便下次继续。',
    positiveText: '关闭',
    negativeText: '取消',
    onPositiveClick: () => {
      emit('update:show', false)
    },
  })
}

const handleComplete = () => {
  if (isWizardGenerating.value || setupLoading.value || stepStages.some(isStageLocked)) return
  markWizardCompleted(props.novelId)
  emit('complete')
  emit('update:show', false)
}
</script>

<style scoped>
:global(.novel-setup-modal) { display: flex; flex-direction: column; overflow: hidden; }
:global(.novel-setup-modal > .n-card-content) { min-height: 0; overflow-y: auto; }
:global(.novel-setup-modal > .n-card__footer),:global(.novel-setup-modal > .n-card-header) { flex-shrink: 0; }
.wizard-footer { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; min-width: 0; }
.wizard-footer__group { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.wizard-footer__group--primary { margin-left: auto; }
.wizard-footer :deep(.n-button) { max-width: 100%; }
.stage-lock-notice { margin-bottom: 12px; }
.stage-restart-button { margin-bottom: 12px; }
.saved-stage-preview { margin-bottom: 16px; padding: 12px; border: 1px solid var(--app-border); border-radius: 8px; }
.saved-stage-preview__item p { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--app-text-secondary); line-height: 1.7; }
.step-content {
  margin: 24px 0;
  min-height: 0;
}

.step-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.step-info {
  text-align: center;
  max-width: 480px;
}

.step-info h3 {
  margin: 16px 0 8px;
  font-size: 20px;
  font-weight: 600;
}

.step-info p {
  color: #666;
  line-height: 1.6;
  margin: 8px 0;
}

.step-panel--storyline {
  align-items: stretch;
  max-width: 100%;
}

.step-info--wide {
  max-width: 100%;
  text-align: center;
}

/* ── 生成中样式 ── */
.step-generating {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.generating-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  border-radius: 12px;
  background: linear-gradient(135deg, #f0f7ff 0%, #e8f5e9 100%);
}

.generating-icon {
  flex-shrink: 0;
}

.generating-text h3 {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 600;
  color: #333;
}

.generating-sub {
  margin: 0;
  font-size: 13px;
  color: #888;
}

/* ── 维度字段卡片 ── */
.dimension-fields {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.field-card {
  background: var(--app-surface, var(--n-color-modal));
  border: 1px solid var(--app-border, var(--n-border-color));
  border-radius: 8px;
  padding: 10px 14px;
  animation: field-appear 0.35s ease;
  transition: border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease;
}

.field-card:hover {
  border-color: var(--color-brand-border, var(--n-primary-color-hover));
}

.field-card--editable {
  padding: 8px 12px;
}

.field-card--editable .field-card__title {
  margin-bottom: 4px;
}

.field-card__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-muted, var(--n-text-color-3));
  margin-bottom: 6px;
  letter-spacing: 0;
  text-transform: uppercase;
}

.field-card__content {
  font-size: 13px;
  line-height: 1.65;
  color: var(--app-text-primary, var(--n-text-color-1));
  white-space: pre-wrap;
  word-break: break-word;
}

.raw-stream-preview {
  min-height: 42px;
  padding: 12px 14px 12px 16px;
  border-radius: 8px;
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-brand, #2563eb) 9%, transparent), transparent 42%),
    var(--app-surface-subtle, var(--n-color-modal));
  border: 1px solid color-mix(in srgb, var(--color-brand, #2563eb) 34%, var(--app-border, rgba(15, 23, 42, 0.12)));
  border-left: 3px solid var(--color-brand, var(--n-primary-color));
  box-shadow: 0 8px 22px color-mix(in srgb, var(--color-brand, #2563eb) 10%, transparent);
  color: var(--app-text-primary, var(--n-text-color-1));
  font-size: 13px;
  font-weight: 500;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.raw-stream-preview::before {
  content: '实时输出';
  display: block;
  width: fit-content;
  margin-bottom: 6px;
  padding: 1px 6px;
  border-radius: 6px;
  background: var(--color-brand-light, rgba(37, 99, 235, 0.08));
  color: var(--color-brand, var(--n-primary-color));
  font-size: 11px;
  font-weight: 700;
}

@keyframes field-appear {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}

.field-card--streaming {
  border-color: color-mix(in srgb, var(--color-brand, #2563eb) 46%, var(--app-border, transparent));
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-brand, #2563eb) 8%, transparent), transparent 48%),
    var(--app-surface, var(--n-color-modal));
  box-shadow: inset 3px 0 0 var(--color-brand, var(--n-primary-color));
}

.streaming-cursor {
  display: inline;
  color: var(--color-brand, var(--n-primary-color));
  animation: blink-cursor 0.8s ease-in-out infinite;
  font-weight: 700;
}

@keyframes blink-cursor {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

/* 文风公约实时预览（生成中） */
.style-preview-generating {
  margin-top: 12px;
  padding: 12px 16px;
  border-radius: 8px;
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-success, #22c55e) 9%, transparent), transparent 45%),
    var(--app-surface-subtle, var(--n-color-modal));
  border: 1px solid color-mix(in srgb, var(--color-success, #22c55e) 34%, var(--app-border, rgba(15, 23, 42, 0.12)));
  border-left: 3px solid var(--color-success, var(--n-success-color));
  animation: fade-in 0.4s ease;
}

.style-preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.style-preview-title {
  font-weight: 500;
  font-size: 14px;
  color: var(--app-text-primary, var(--n-text-color-1));
  flex: 1;
}

.style-preview-content {
  font-size: 13px;
  line-height: 1.6;
  color: var(--app-text-primary, var(--n-text-color-1));
  padding-left: 24px;
}

@keyframes fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ── 流式卡片（人物） ── */
.streaming-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 8px;
}

.char-card {
  padding: 14px 16px;
  border-radius: 10px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
  transition: all 0.35s ease;
}

.char-card--filled {
  border-color: #18a05830;
  background: #18a05806;
}

.char-card--loading {
  border-style: dashed;
  border-color: #2080f040;
  background: #2080f004;
}

.char-card__header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.char-card__avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  font-weight: 600;
  flex-shrink: 0;
}

.char-card__avatar--protag {
  background: linear-gradient(135deg, #f5af19 0%, #f12711 100%);
  box-shadow: 0 0 0 2px #f5af1930;
}

.char-card__avatar--skeleton {
  background: #f0f0f0;
  color: transparent;
}

.char-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.char-card__name {
  font-weight: 600;
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.char-card__desc {
  font-size: 13px;
  color: var(--app-text-secondary, var(--n-text-color-2));
  line-height: 1.6;
  margin-top: 8px;
  padding-left: 46px;
}

.char-card__anchor {
  display: flex;
  gap: 6px;
  align-items: baseline;
  margin-top: 6px;
  padding-left: 46px;
  color: var(--app-text-primary, var(--n-text-color-1));
  font-size: 12px;
  line-height: 1.5;
}

.char-card__anchor-label {
  flex: 0 0 auto;
  padding: 1px 6px;
  border-radius: 6px;
  background: var(--color-brand-light, rgba(37, 99, 235, 0.08));
  color: var(--color-brand, var(--n-primary-color));
  font-weight: 700;
}

.char-card__relations {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
  padding-left: 46px;
}

.char-card__skeleton-bar {
  display: inline-block;
  height: 14px;
  border-radius: 4px;
  background: linear-gradient(90deg, #f0f0f0 25%, #e4e4e4 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s ease-in-out infinite;
}

.char-card__skeleton-bar--tag {
  width: 48px;
  height: 20px;
  border-radius: 10px;
}

.char-card__skeleton-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
  padding-left: 46px;
}

@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* ── 流式卡片（地点） ── */
.streaming-loc-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 8px;
}

.loc-card {
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-modal);
  transition: all 0.35s ease;
}

.loc-card--filled {
  border-color: #2080f030;
  background: #2080f006;
}

.loc-card--loading {
  border-style: dashed;
  border-color: #f0a02040;
  background: #f0a02004;
}

.loc-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.loc-card__icon {
  font-size: 18px;
  flex-shrink: 0;
}

.loc-card__icon--skeleton {
  width: 18px;
  height: 18px;
  border-radius: 4px;
  background: #f0f0f0;
  animation: shimmer 1.5s ease-in-out infinite;
  background-size: 200% 100%;
}

.loc-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.loc-card__name {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.loc-card__desc {
  font-size: 13px;
  color: #666;
  line-height: 1.5;
  margin-top: 6px;
  padding-left: 26px;
}

.loc-card__skeleton-bar {
  display: inline-block;
  height: 12px;
  border-radius: 4px;
  background: linear-gradient(90deg, #f0f0f0 25%, #e4e4e4 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s ease-in-out infinite;
}

.loc-card__skeleton-body {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 8px;
  padding-left: 26px;
}

/* ── 动画 ── */
.fade-slide-enter-active {
  transition: all 0.4s ease;
}

.fade-slide-leave-active {
  transition: all 0.2s ease;
}

.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(12px);
}

.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}

/* ── 其他 ── */
.bible-preview {
  width: 100%;
}

.plot-options-block,
.plot-custom-block {
  width: 100%;
}

.plot-outline-generating {
  width: 100%;
}

.plot-outline-progress {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.plot-outline-progress__item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--app-border, var(--n-border-color));
  border-radius: 8px;
  background: var(--app-surface, var(--n-color-modal));
}

.plot-outline-progress__dot {
  width: 9px;
  height: 9px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--app-border, var(--n-border-color));
  flex: 0 0 auto;
}

.plot-outline-progress__item--active {
  border-color: color-mix(in srgb, var(--color-brand, #2563eb) 42%, var(--app-border, transparent));
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-brand, #2563eb) 7%, transparent), transparent 50%),
    var(--app-surface, var(--n-color-modal));
}

.plot-outline-progress__item--active .plot-outline-progress__dot {
  background: var(--color-brand, var(--n-primary-color));
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--color-brand, #2563eb) 12%, transparent);
  animation: plot-progress-pulse 1.2s ease-in-out infinite;
}

.plot-outline-progress__item--done .plot-outline-progress__dot {
  background: var(--color-success, var(--n-success-color));
}

.plot-outline-progress__body {
  min-width: 0;
}

.plot-outline-progress__label {
  font-size: 13px;
  font-weight: 700;
  color: var(--app-text-primary, var(--n-text-color-1));
  line-height: 1.4;
}

.plot-outline-progress__desc {
  margin-top: 2px;
  font-size: 12px;
  color: var(--app-text-muted, var(--n-text-color-3));
  line-height: 1.45;
}

.plot-outline-live-preview {
  max-height: 180px;
  overflow: auto;
}

@keyframes plot-progress-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.68; transform: scale(0.92); }
}

.wizard-error-text {
  white-space: pre-line;
  line-height: 1.65;
  font-size: 13px;
}

.wizard-hint-alert {
  line-height: 1.55;
  text-align: left;
}

.plot-option-title {
  font-weight: 600;
  font-size: 15px;
}

.plot-line {
  font-size: 13px;
  line-height: 1.55;
  color: #555;
  text-align: left;
}

.plot-outline-editor {
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.plot-outline-editor :deep(.n-form-item-label) {
  font-weight: 600;
}

.plot-kv-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.plot-kv-label {
  width: fit-content;
  padding: 1px 7px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-brand, #2563eb) 8%, transparent);
  color: var(--app-text-secondary, var(--n-text-color-2));
  font-size: 12px;
  font-weight: 700;
}

.plot-guard-grid,
.plot-subline-list {
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.03);
  text-align: left;
}

.plot-guard-grid {
  display: grid;
  gap: 6px;
}

.plot-guard-cell {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  font-size: 12px;
  line-height: 1.55;
}

.plot-guard-k {
  color: #777;
  font-weight: 700;
}

.plot-guard-v {
  color: #555;
}

.plot-subline-title {
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 700;
  color: #666;
}

.plot-subline-item {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
  line-height: 1.5;
  color: #555;
}

.plot-subline-item + .plot-subline-item {
  margin-top: 5px;
}

.plot-subline-name {
  font-weight: 600;
}

.plot-subline-purpose {
  color: #777;
}

.plot-outline-stage-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.plot-stage-edit-item {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  padding: 10px;
  border-radius: 8px;
  background: var(--app-surface, var(--n-color-modal));
  border: 1px solid var(--app-border, var(--n-border-color));
}

.plot-stage-edit-item + .plot-stage-edit-item {
  margin-top: 0;
}

.plot-stage-edit-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plot-stage-chapter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plot-stage-chapter-row :deep(.n-input-number) {
  width: 112px;
}

.plot-stage-chapter-separator {
  color: var(--app-text-muted, var(--n-text-color-3));
  font-size: 13px;
}

.plot-option-card--disabled {
  opacity: 0.72;
  pointer-events: none;
}

.style-convention-text {
  white-space: pre-wrap;
  line-height: 1.65;
  font-size: 14px;
}

/* (editable-field 已替换为 field-card) */

.editable-character,
.editable-location {
  width: 100%;
  padding: 4px 0;
}

.character-editor-list :deep(.n-list-item__main) {
  width: 100%;
}

.character-editor-head {
  display: grid;
  grid-template-columns: minmax(120px, 180px) minmax(100px, 150px) auto;
  gap: 8px;
  align-items: center;
}

.character-editor-head__name,
.character-editor-head__role {
  min-width: 0;
}

.role-lock-panel {
  height: 100%;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--app-border, var(--n-border-color));
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--color-brand, #2563eb) 5%, transparent), transparent 42%),
    var(--app-surface, var(--n-color-modal));
}

.role-lock-panel--strong {
  border-color: color-mix(in srgb, var(--color-brand, #2563eb) 30%, var(--app-border, rgba(15, 23, 42, 0.12)));
  box-shadow: inset 3px 0 0 var(--color-brand, var(--n-primary-color));
}

.role-lock-panel__title {
  margin-bottom: 8px;
  font-size: 12px;
  font-weight: 700;
  color: var(--color-brand, var(--n-primary-color));
}

.editable-field {
  width: 100%;
  margin-top: 8px;
}
.editable-field__label {
  font-size: 12px;
  color: var(--app-text-muted, var(--n-text-color-3));
  margin-bottom: 4px;
  line-height: 1.4;
}

.character-meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.voice-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin-top: 8px;
}

.relationship-editor {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.relationship-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.4fr) auto;
  gap: 6px;
  align-items: center;
}

.wound-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.wound-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}

@media (max-width: 720px) {
  .character-editor-head {
    grid-template-columns: 1fr;
  }

  .plot-outline-progress {
    grid-template-columns: 1fr;
  }

  .voice-grid,
  .character-meta-grid,
  .relationship-row,
  .wound-row {
    grid-template-columns: 1fr;
  }
}

/* 步骤导航可点击 */
.wizard-steps :deep(.n-step) {
  cursor: default;
}
.wizard-step-clickable {
  cursor: pointer !important;
}
.wizard-step-clickable:hover :deep(.n-step-indicator) {
  box-shadow: 0 0 0 3px rgba(24, 160, 88, 0.15);
}
</style>
