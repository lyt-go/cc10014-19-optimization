<template>
  <div v-if="panel.showPanel.value" class="modal-backdrop" data-testid="signing-panel">
    <section class="modal signing-modal">
      <header class="signing-head">
        <h3>签署审批待办 · 复检依据</h3>
        <button class="link" type="button" data-testid="signing-close" @click="panel.close()">关闭</button>
      </header>

      <p v-if="panel.loading.value" class="muted">正在定位同工序、同检查项目的复检…</p>

      <div v-else-if="panel.data.value" class="signing-body">
        <p class="muted">
          验收工序：<strong>{{ panel.data.value['验收工序'] }}</strong>
          ｜原缺陷 {{ panel.data.value.defects.length }} 项，仍待处理
          <strong :class="panel.allCovered.value ? 'ok' : 'warn'">
            {{ panel.pendingItems.value.length }}
          </strong> 项
        </p>

        <!-- 可勾选的复检：编号与检查清单一致 -->
        <h4 class="block-title">可用于签署的复检</h4>
        <table v-if="panel.selectable.value.length" class="mini-table">
          <thead>
            <tr><th>勾选</th><th>复检编号</th><th>原检查编号</th><th>检查项目</th><th>检查人</th><th>复检结果</th></tr>
          </thead>
          <tbody>
            <tr v-for="opt in panel.selectable.value" :key="opt.复检id">
              <td>
                <input
                  type="checkbox"
                  :data-testid="`pick-${opt.复检编号}`"
                  :checked="panel.selectedIds.value.includes(opt.复检id)"
                  @change="panel.toggle(opt.复检id)"
                />
              </td>
              <td>{{ opt.复检编号 }}</td>
              <td>{{ opt.原检查编号 }}</td>
              <td>{{ opt.检查项目 }}</td>
              <td>{{ opt.检查人 }}</td>
              <td>{{ opt.复检结果 }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">当前没有可签署的复检（合格复检可能正在更正或人/项目/工序对不上）。</p>

        <!-- 不可用复检及原因 -->
        <template v-if="panel.blocked.value.length">
          <h4 class="block-title">不能补缺的通过记录</h4>
          <ul class="blocked-list">
            <li v-for="b in panel.blocked.value" :key="b.复检id" data-testid="blocked-item">
              <span class="fj-no">{{ b.复检编号 }}</span>
              <span class="reason">{{ b.不可用原因 }}</span>
            </li>
          </ul>
        </template>

        <!-- 未被复检覆盖、保持待处理的项目 -->
        <template v-if="panel.pendingItems.value.length">
          <h4 class="block-title">未被复检覆盖（保持待处理）</h4>
          <ul class="pending-list">
            <li v-for="p in panel.pendingItems.value" :key="p.原检查编号" data-testid="pending-item">
              {{ p.原检查编号 }} / {{ p.检查项目 }}<span v-if="p.缺陷">：{{ p.缺陷 }}</span>
            </li>
          </ul>
        </template>

        <!-- 签署回执：交代依据并指明能否归档 -->
        <div v-if="panel.receipt.value" class="receipt" data-testid="signing-receipt">
          <h4>签署回执 {{ panel.receipt.value['回执编号'] }}</h4>
          <p><span class="muted">依据复检：</span>{{ panel.receipt.value['依据复检'] }}</p>
          <p :class="panel.receipt.value['可归档'] ? 'ok' : 'warn'">
            {{ panel.receipt.value['回执说明'] }}
          </p>
        </div>

        <p v-if="panel.errorMessage.value" class="error-text" data-testid="signing-error">
          {{ panel.errorMessage.value }}
        </p>

        <footer class="signing-foot" v-if="!panel.receipt.value">
          <span class="muted">已选 {{ panel.selectedIds.value.length }} 条复检</span>
          <button
            class="btn primary"
            type="button"
            data-testid="signing-submit"
            :disabled="panel.submitting.value || panel.selectedIds.value.length === 0"
            @click="panel.submit()"
          >
            {{ panel.submitting.value ? '提交中…' : '按勾选复检发起签署' }}
          </button>
        </footer>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import type { useSigningPanel } from '@/composables/useSigningPanel'

defineProps<{ panel: ReturnType<typeof useSigningPanel> }>()
</script>

<style scoped>
.signing-modal { width: min(820px, 96vw); }
.signing-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.signing-head h3 { margin: 0; }
.block-title { font-size: 13px; margin: 14px 0 6px; color: #334155; }
.mini-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mini-table th, .mini-table td { border: 1px solid var(--border); padding: 6px 8px; text-align: left; }
.blocked-list, .pending-list { margin: 0; padding-left: 18px; font-size: 12px; }
.blocked-list li, .pending-list li { margin-bottom: 4px; }
.fj-no { font-weight: 600; margin-right: 6px; }
.reason { color: #b42318; }
.receipt { margin-top: 14px; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: #f8fafc; }
.receipt h4 { margin: 0 0 6px; }
.signing-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; }
.muted { color: var(--muted); font-size: 12px; }
.ok { color: #15803d; font-weight: 600; }
.warn { color: #b45309; font-weight: 600; }
</style>
