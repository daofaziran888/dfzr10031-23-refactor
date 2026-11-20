<template>
  <section class="page" data-module="fetch_service">
    <header class="page-head">
      <div>
        <h2>取数服务 · 参数绑定</h2>
        <p class="page-desc">
          把 OLAP 查询与数据 API 的取数条件收敛成一份参数绑定清单：保留输入值与业务类型，
          区分未填写、空字符串和数值零，跨服务执行前先看空值含义是否一致。
        </p>
      </div>
    </header>

    <article class="panel">
      <header class="panel-head">
        <h3>1. 旧列表与新报表对照</h3>
        <span class="hint">旧列表保持原语义：空串不筛选、零是有效值；新报表单独记录空串含义。</span>
      </header>
      <div class="filter-bar">
        <label class="filter-item">
          <span>旧列表 keyword</span>
          <input v-model="legacyKeyword" placeholder="模板编号" />
        </label>
        <button class="btn" type="button" @click="runLegacyQuery()">查询旧列表</button>
        <button class="btn ghost" type="button" @click="runLegacyPreset('')">按空串</button>
        <button class="btn ghost" type="button" @click="runLegacyPreset('0')">按零</button>
        <button class="btn ghost" type="button" @click="runLegacyPreset(undefined)">按未填</button>
      </div>
      <div class="compare-grid">
        <div class="compare-card">
          <h4>旧列表（OLAP查询）</h4>
          <p v-if="legacyResult">
            命中 {{ legacyResult.total }} 条：{{ legacyResult.codes.join('、') || '无' }}
          </p>
          <p v-else class="hint">尚未查询</p>
        </div>
        <div class="compare-card">
          <h4>新报表（取数服务）</h4>
          <p v-if="legacyCoverage">
            {{ legacyCoverage.records }} 行 / {{ legacyCoverage.group_count }} 个商品分组，
            空值记录 {{ legacyCoverage.null_record_count }} 条
          </p>
          <p v-else class="hint">尚未取数</p>
        </div>
      </div>
    </article>

    <article class="panel">
      <header class="panel-head">
        <h3>2. 参数绑定清单</h3>
        <span class="hint">每个参数单独选择“未填 / 空串 / 值”，清单保留输入原文与业务类型。</span>
      </header>
      <div class="filter-bar">
        <label class="filter-item">
          <span>取数模板</span>
          <select v-model="selectedTemplate">
            <option v-for="item in templates" :key="keyOf(item)" :value="keyOf(item)">
              {{ item.template_code }} · {{ item.template_name }}
            </option>
          </select>
        </label>
        <button class="btn primary" type="button" @click="loadManifest">生成绑定清单</button>
      </div>

      <table v-if="activeTemplate" class="data-table">
        <thead>
          <tr>
            <th>变量</th><th>来源字段</th><th>业务类型</th><th>输入形态</th><th>输入值</th>
            <th>空串含义</th><th>算子</th><th>范围</th><th>是否实际使用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="condition in activeTemplate.conditions" :key="condition.variable">
            <td>{{ condition.variable }}</td>
            <td>{{ condition.field }}</td>
            <td>{{ condition.type ?? 'string' }}</td>
            <td>
              <select v-model="paramForms[condition.variable]">
                <option value="missing">未填</option>
                <option value="empty">空串</option>
                <option value="zero">零</option>
                <option value="value">有值</option>
              </select>
            </td>
            <td>
              <input
                v-if="paramForms[condition.variable] === 'value'"
                v-model="paramValues[condition.variable]"
                class="inline-input"
                placeholder="输入值"
              />
              <span v-else class="hint">{{ presenceLabel(paramForms[condition.variable]) }}</span>
            </td>
            <td>{{ condition.empty_means ?? 'unconfirmed' }}</td>
            <td>{{ bindingOf(condition.variable)?.operator ?? '—' }}</td>
            <td>{{ bindingOf(condition.variable)?.scope ?? '—' }}</td>
            <td>{{ condition.used === false ? '否（查询未引用）' : '是' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="manifestError" class="error-text">{{ manifestError }}</p>
      <p v-else-if="manifest" class="hint">
        请求指纹 {{ manifest.request_key }}；待复核变量：
        {{ manifest.ambiguous_bindings.map((item: Binding) => item.variable).join('、') || '无' }}
      </p>
    </article>

    <article class="panel">
      <header class="panel-head">
        <h3>3. 跨服务策略比较</h3>
        <span class="hint">
          OLAP 历史模板把空串当“不筛选”，数据API 另一模板当“字段为空”。比较返回全组与暂缓确认。
        </span>
      </header>
      <div class="filter-bar">
        <button class="btn" type="button" @click="runCompare">比较 all / defer</button>
        <button class="btn ghost" type="button" @click="runConfirm">先确认再发布（confirm）</button>
        <label class="filter-item">
          <span>确认空串含义</span>
          <select v-model="confirmation">
            <option value="">未确认</option>
            <option value="all">不筛选</option>
            <option value="empty">字段为空</option>
          </select>
        </label>
      </div>
      <div v-if="compareRows.length" class="compare-grid">
        <div v-for="row in compareRows" :key="row.policy" class="compare-card">
          <h4>{{ row.policy }}</h4>
          <p>状态：{{ row.status }}</p>
          <p>商品分组：{{ row.group_count }} 个</p>
          <p>覆盖行数：{{ row.records }}</p>
          <p>空值记录：{{ row.null_record_count }}</p>
          <p class="hint">待确认模板：{{ row.pending.join('、') || '无' }}</p>
        </div>
      </div>
      <p v-if="compareError" class="error-text">{{ compareError }}</p>
    </article>

    <article class="panel">
      <header class="panel-head">
        <h3>4. 报表交付与回查</h3>
        <span class="hint">交付后模板定义再更正，只追加按当时参数的重算结果，不改已交付覆盖。</span>
      </header>
      <div class="filter-bar">
        <button class="btn primary" type="button" @click="createReport">创建并交付报表</button>
        <button class="btn" type="button" @click="recalculateReport" :disabled="!report">按当时参数重算</button>
      </div>
      <div v-if="report" class="compare-grid">
        <div class="compare-card">
          <h4>交付时覆盖</h4>
          <p>{{ report.delivered?.coverage?.records ?? report.coverage.records }} 行</p>
          <p>分组：{{ (report.delivered?.coverage?.product_groups ?? report.coverage.product_groups).join('、') }}</p>
          <p class="hint">定义版本：{{ versionText(report.definition_versions_at_delivery) }}</p>
        </div>
        <div class="compare-card">
          <h4>最近一次重算</h4>
          <p v-if="report.latest_recalculation">
            {{ report.latest_recalculation.coverage.records }} 行，
            {{ report.latest_recalculation.changed ? '覆盖结果已变化' : '覆盖结果未变' }}
          </p>
          <p v-else class="hint">暂无重算记录</p>
        </div>
      </div>
      <p v-if="reportError" class="error-text">{{ reportError }}</p>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { fetchJson, request } from '@/api/client'

type Condition = {
  variable: string
  field: string
  type?: string
  empty_means?: string
  scope?: string
  used?: boolean
}

type TemplateView = {
  module: string
  template_id: number
  template_code: string
  template_name: string
  scope: string
  conditions: Condition[]
  version: number
  empty_meanings: string[]
  ignored_variables: string[]
  needs_review: boolean
}

type Binding = {
  variable: string
  source_field: string
  business_type: string
  input_value: unknown
  presence: string
  scope: string
  operator: string
  empty_means: string
  used_by_query: boolean
  annotation: string
}

type Coverage = {
  status: string
  policy: string
  records: number
  product_groups: string[]
  group_count: number
  null_record_count: number
  request_key: string
  pending_templates: { template_code: string }[]
  notes: string[]
}

type ReportView = {
  id: number
  status: string
  coverage: Coverage
  delivered: { coverage: Coverage } | null
  latest_recalculation: { coverage: Coverage; changed: boolean } | null
  definition_versions_at_delivery: Record<string, number> | null
}

const templates = ref<TemplateView[]>([])
const selectedTemplate = ref('')
const paramForms = ref<Record<string, string>>({})
const paramValues = ref<Record<string, string>>({})
const manifest = ref<{ request_key: string; ambiguous_bindings: Binding[]; bindings: Binding[] } | null>(null)
const manifestError = ref('')

const legacyKeyword = ref('')
const legacyResult = ref<{ total: number; codes: string[] } | null>(null)
const legacyCoverage = ref<Coverage | null>(null)

const confirmation = ref('')
const compareRows = ref<
  { policy: string; status: string; group_count: number; records: number; null_record_count: number; pending: string[] }[]
>([])
const compareError = ref('')

const report = ref<ReportView | null>(null)
const reportError = ref('')

const activeTemplate = computed(() =>
  templates.value.find((item) => keyOf(item) === selectedTemplate.value) ?? null,
)

const crossTemplates = computed(() => [
  { module: 'query_builder', template_id: 1, params: { category: '' } },
  { module: 'data_service', template_id: 1, params: { category: '' } },
])

function keyOf(item: { module: string; template_id: number }) {
  return `${item.module}:${item.template_id}`
}

function presenceLabel(form: string) {
  return { missing: '未填写', empty: '空字符串', zero: '数值零', value: '有值' }[form] ?? form
}

function bindingOf(variable: string): Binding | undefined {
  return manifest.value?.bindings.find((item) => item.variable === variable)
}

function confirmationPayload() {
  return confirmation.value ? { category: confirmation.value } : {}
}

function paramsForForm(condition: Condition): Record<string, unknown> {
  const form = paramForms.value[condition.variable] ?? 'missing'
  if (form === 'missing') return {}
  if (form === 'empty') return { [condition.variable]: '' }
  if (form === 'zero') return { [condition.variable]: 0 }
  const raw = paramValues.value[condition.variable] ?? ''
  const value = condition.type === 'number' && raw.trim() !== '' ? Number(raw) : raw
  return { [condition.variable]: value }
}

function currentParams() {
  if (!activeTemplate.value) return {}
  return activeTemplate.value.conditions.reduce<Record<string, unknown>>((acc, condition) => {
    if (condition.used === false) return acc
    return { ...acc, ...paramsForForm(condition) }
  }, {})
}

async function loadTemplates() {
  const payload = await fetchJson<{ items: TemplateView[] }>('/api/fetch/templates')
  templates.value = payload.items
  if (!selectedTemplate.value && templates.value.length) {
    selectedTemplate.value = keyOf(templates.value[0])
  }
  resetParams()
}

function resetParams() {
  const nextForms: Record<string, string> = {}
  const nextValues: Record<string, string> = {}
  for (const condition of activeTemplate.value?.conditions ?? []) {
    nextForms[condition.variable] = 'missing'
    nextValues[condition.variable] = ''
  }
  paramForms.value = nextForms
  paramValues.value = nextValues
  manifest.value = null
  manifestError.value = ''
}

async function loadManifest() {
  if (!activeTemplate.value) return
  manifestError.value = ''
  try {
    const response = await request('/api/fetch/parameter-bindings', {
      method: 'POST',
      body: JSON.stringify({
        values: {
          module: activeTemplate.value.module,
          template_id: activeTemplate.value.template_id,
          params: currentParams(),
          confirmations: confirmationPayload(),
        },
      }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '绑定清单生成失败')
    manifest.value = payload
  } catch (error) {
    manifest.value = null
    manifestError.value = error instanceof Error ? error.message : '绑定清单生成失败'
  }
}

function runLegacyPreset(keyword?: string) {
  legacyKeyword.value = keyword ?? ''
  void runLegacyQuery(keyword)
}

async function runLegacyQuery(keywordOverride?: string) {
  const keyword = keywordOverride ?? legacyKeyword.value
  const search = new URLSearchParams()
  if (keyword !== undefined) search.set('keyword', keyword)
  const payload = await fetchJson<{ total: number; items: { 模板编号: string }[] }>(
    `/api/query_builder?${search.toString()}`,
  )
  legacyResult.value = { total: payload.total, codes: payload.items.map((row) => row.模板编号) }

  const response = await request('/api/fetch/coverage', {
    method: 'POST',
    body: JSON.stringify({
      values: {
        templates: [{ module: 'query_builder', template_id: 3, params: { amount: keyword === '0' ? 0 : keyword === '' ? '' : undefined } }],
        policy: 'local',
      },
    }),
  })
  const coverage = await response.json()
  legacyCoverage.value = response.ok ? coverage : null
}

async function runCompare() {
  compareError.value = ''
  try {
    const payload = await fetchJson<{ comparisons: Record<string, Coverage & { code?: string; message?: string }> }>(
      '/api/fetch/compare',
      {
        method: 'POST',
        body: JSON.stringify({ values: { templates: crossTemplates.value } }),
      },
    )
    compareRows.value = Object.entries(payload.comparisons).map(([policy, item]) => ({
      policy,
      status: item.status,
      group_count: item.group_count ?? 0,
      records: item.records ?? 0,
      null_record_count: item.null_record_count ?? 0,
      pending: (item.pending_templates ?? []).map((entry) => entry.template_code),
    }))
  } catch (error) {
    compareRows.value = []
    compareError.value = error instanceof Error ? error.message : '策略比较失败'
  }
}

async function runConfirm() {
  compareError.value = ''
  try {
    const coverage = await fetchJson<Coverage>('/api/fetch/coverage', {
      method: 'POST',
      body: JSON.stringify({
        values: {
          templates: crossTemplates.value,
          policy: 'confirm',
          confirmations: confirmationPayload(),
        },
      }),
    })
    compareRows.value = [
      {
        policy: 'confirm',
        status: coverage.status,
        group_count: coverage.group_count,
        records: coverage.records,
        null_record_count: coverage.null_record_count,
        pending: [],
      },
    ]
  } catch (error) {
    compareRows.value = []
    compareError.value = error instanceof Error ? error.message : '确认执行失败'
  }
}

async function createReport() {
  reportError.value = ''
  try {
    const response = await request('/api/fetch/reports', {
      method: 'POST',
      body: JSON.stringify({
        values: {
          name: '商品覆盖对照报表',
          templates: crossTemplates.value,
          policy: 'local',
        },
      }),
    })
    const created = await response.json()
    if (!response.ok) throw new Error(created.message || '报表创建失败')
    report.value = created
    // 含未确认空值含义的报表会被后端拦下，页面保留草稿并显示原因。
    report.value = await fetchJson<ReportView>(`/api/fetch/reports/${created.id}/deliver`, {
      method: 'POST',
    })
  } catch (error) {
    report.value = null
    reportError.value = error instanceof Error ? error.message : '报表创建失败'
  }
}

async function recalculateReport() {
  if (!report.value) return
  reportError.value = ''
  try {
    await fetchJson<unknown>(`/api/fetch/reports/${report.value.id}/recalculate`, { method: 'POST' })
    report.value = await fetchJson<ReportView>(`/api/fetch/reports/${report.value.id}`)
  } catch (error) {
    reportError.value = error instanceof Error ? error.message : '重算失败'
  }
}

function versionText(versions: Record<string, number> | null | undefined) {
  if (!versions) return '—'
  return Object.entries(versions)
    .map(([key, value]) => `${key} v${value}`)
    .join('；')
}

watch(selectedTemplate, () => {
  resetParams()
})

onMounted(async () => {
  try {
    await loadTemplates()
  } catch (error) {
    manifestError.value = error instanceof Error ? error.message : '取数模板读取失败'
  }
})
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; }
.panel-head { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.panel-head h3 { margin: 0; font-size: 15px; }
.hint { color: var(--muted); font-size: 12px; }
.compare-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.compare-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: #fbfcfe; }
.compare-card h4 { margin: 0 0 8px; font-size: 13px; }
.compare-card p { margin: 4px 0; font-size: 13px; }
.inline-input { padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; width: 120px; }
</style>
