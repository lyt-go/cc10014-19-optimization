import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
  extra: Record<string, unknown> = {},
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action, ...extra }),
  })
  return await response.json()
}

/** 验收签署候选：同工序、同检查项目可用于签署的复检及不可用原因。 */
export type SigningOption = {
  复检id: number
  复检编号: string
  原检查编号: string
  检查工序: string
  检查项目: string
  检查人: string
  检查日期: string
  复检结果: string
  status: string
  可覆盖缺陷?: string
  不可用原因?: string
}

export type SigningOptions = {
  acceptance_id: number
  验收工序: string
  options: SigningOption[]
  unavailable: SigningOption[]
  defects: { 原检查编号: string; 检查项目: string; 检查人: string; 缺陷: string; 待处理: boolean }[]
  pending_items: { 原检查编号: string; 检查项目: string; 缺陷: string }[]
}

export async function fetchSigningOptions(acceptanceId: number): Promise<SigningOptions> {
  const response = await request(`/api/acceptance_record/${acceptanceId}/signing-options`)
  if (!response.ok) throw new Error('复检候选读取失败')
  return await response.json()
}

export async function signAcceptance(
  acceptanceId: number,
  reinspectIds: number[],
): Promise<ActionResult> {
  const response = await request(`/api/acceptance_record/${acceptanceId}/sign`, {
    method: 'POST',
    body: JSON.stringify({ action: '签署验收', reinspect_ids: reinspectIds }),
  })
  return await response.json()
}

export async function correctReinspect(
  reinspectId: number,
  stage: 'begin' | 'complete',
  payload: { 更正原因?: string; 复检结果?: string } = {},
): Promise<ActionResult> {
  const response = await request(`/api/reinspect/${reinspectId}/correction`, {
    method: 'POST',
    body: JSON.stringify({ ...payload, remark: stage }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type DependentStatus = {
  module: string
  entry_id: number
  entry: Record<string, unknown>
  upstream: { module: string; id: number; name: string; status: string }[]
  downstream: { module: string; id: number; name: string; status: string }[]
}

export async function fetchDependentStatus(
  moduleKey: string,
  entryId: number,
): Promise<DependentStatus> {
  const response = await request(`/api/cross-module/dependent-status/${moduleKey}/${entryId}`)
  if (!response.ok) throw new Error('跨模块状态查询失败')
  return await response.json()
}

export async function fetchDependencyGraph(): Promise<{ nodes: unknown[]; edges: unknown[] }> {
  const response = await request('/api/cross-module/dependency-graph')
  if (!response.ok) throw new Error('依赖图加载失败')
  return await response.json()
}
