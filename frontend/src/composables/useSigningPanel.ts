import { computed, ref } from 'vue'
import {
  correctReinspect,
  fetchSigningOptions,
  signAcceptance,
  type SigningOptions,
} from '@/api/backend-service'

/**
 * 验收签署待办：从验收记录定位同工序、同检查项目可签署的复检，
 * 勾选的复检结果同步到签署；展示不可用原因、待处理项目和签署回执。
 */
export function useSigningPanel(reload: () => Promise<void>) {
  const showPanel = ref(false)
  const loading = ref(false)
  const submitting = ref(false)
  const currentRow = ref<Record<string, unknown> | null>(null)
  const data = ref<SigningOptions | null>(null)
  const selectedIds = ref<number[]>([])
  const errorMessage = ref('')
  const receipt = ref<Record<string, unknown> | null>(null)

  const selectable = computed(() => data.value?.options ?? [])
  const blocked = computed(() => data.value?.unavailable ?? [])
  const pendingItems = computed(() => data.value?.pending_items ?? [])
  const allCovered = computed(() => pendingItems.value.length === 0)

  async function open(row: Record<string, unknown>) {
    currentRow.value = row
    showPanel.value = true
    receipt.value = null
    errorMessage.value = ''
    selectedIds.value = []
    loading.value = true
    try {
      data.value = await fetchSigningOptions(Number(row.id))
    } catch (e) {
      errorMessage.value = e instanceof Error ? e.message : '复检候选读取失败'
      data.value = null
    } finally {
      loading.value = false
    }
  }

  function toggle(id: number) {
    const idx = selectedIds.value.indexOf(id)
    if (idx >= 0) selectedIds.value.splice(idx, 1)
    else selectedIds.value.push(id)
  }

  async function submit() {
    if (!currentRow.value) return
    submitting.value = true
    errorMessage.value = ''
    try {
      const r = await signAcceptance(Number(currentRow.value.id), [...selectedIds.value])
      if (!r.ok) throw new Error(r.message)
      receipt.value = (r.entry as Record<string, unknown>)?.['签署回执'] as Record<string, unknown>
      await reload()
    } catch (e) {
      errorMessage.value = e instanceof Error ? e.message : '签署失败'
    } finally {
      submitting.value = false
    }
  }

  async function beginCorrection(id: number, reason: string) {
    const r = await correctReinspect(id, 'begin', { 更正原因: reason })
    if (!r.ok) throw new Error(r.message)
    if (currentRow.value) await open(currentRow.value)
    return r
  }

  async function completeCorrection(id: number, result: string) {
    const r = await correctReinspect(id, 'complete', { 复检结果: result })
    if (!r.ok) throw new Error(r.message)
    if (currentRow.value) await open(currentRow.value)
    return r
  }

  function close() {
    showPanel.value = false
    currentRow.value = null
    data.value = null
    receipt.value = null
    selectedIds.value = []
    errorMessage.value = ''
  }

  return {
    showPanel, loading, submitting, currentRow, data, selectedIds, errorMessage, receipt,
    selectable, blocked, pendingItems, allCovered,
    open, toggle, submit, close, beginCorrection, completeCorrection,
  }
}
