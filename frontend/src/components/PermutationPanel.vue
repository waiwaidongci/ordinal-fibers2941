<script setup lang="ts">
import {computed} from 'vue';
import type {Permutation,Result} from '../types/domain';
import {num,pvalue} from '../utils/format';
const props=defineProps<{permutation:Permutation;result:Result}>();
const pct=computed(()=>{const t=props.permutation.target_allocations||0;return t?Math.min(100,(props.permutation.completed_allocations/t)*100):0;});
const isMc=computed(()=>props.permutation.mode==='monte_carlo');
const done=computed(()=>props.permutation.status==='completed'&&props.permutation.p_value!=null);
</script>
<template>
  <section class="perm-panel">
    <h2 class="ruled-title">可复算置换检验 <small>保持各工艺试样数，在合并平均秩上重排同一 H 统计量</small></h2>
    <div class="perm-grid">
      <div>
        <dl class="perm-facts">
          <div><dt>检验方式</dt><dd>{{isMc?'固定种子 Monte Carlo 抽样':'全部分配穷举（exact）'}}</dd></div>
          <div><dt>预设种子</dt><dd>{{permutation.seed}}</dd></div>
          <div><dt>保持试样数的分配总数</dt><dd>{{permutation.n_allocations.toLocaleString('zh-CN')}}<small>log₁₀≈{{num(permutation.log_n_allocations/Math.LN10,2)}}；各组 n = {{permutation.group_sizes.join(' / ')}}</small></dd></div>
          <div v-if="isMc"><dt>随机抽样次数</dt><dd>{{permutation.target_allocations.toLocaleString('zh-CN')}}<small>分配总数超过穷举阈值，改按预设种子抽样</small></dd></div>
          <div v-else><dt>穷举分配数</dt><dd>{{permutation.target_allocations.toLocaleString('zh-CN')}}</dd></div>
          <div><dt>极端分配计数（H<sub>perm</sub> ≥ H<sub>obs</sub>，含并列）</dt><dd>{{permutation.extreme_count.toLocaleString('zh-CN')}}<small>观察分配自身计入</small></dd></div>
        </dl>
      </div>
      <div>
        <div class="perm-progress-head">
          <small>计算进度</small>
          <b>{{permutation.completed_allocations.toLocaleString('zh-CN')}} / {{permutation.target_allocations.toLocaleString('zh-CN')}}</b>
          <span>{{num(pct,1)}}%</span>
        </div>
        <div class="perm-progress-track" role="progressbar" :aria-valuenow="permutation.completed_allocations" :aria-valuemax="permutation.target_allocations" aria-valuemin="0">
          <div class="perm-progress-fill" :class="{done}" :style="{width:pct+'%'}"></div>
        </div>
        <p class="muted" v-if="!done">每批 {{permutation.batch_size}} 次分配原子提交一次检查点；worker 中断后从最后检查点续算，已计数分配不重复，结果与不中断运行一致。已恢复 {{permutation.resume_count}} 次。</p>
        <p class="muted" v-else>检查点已闭环，共恢复 {{permutation.resume_count}} 次；同种子重跑或中断续算均得到同一 p 值。</p>
      </div>
    </div>

    <div class="perm-conclusions">
      <div class="perm-conclusion" :class="result.omnibus_reject?'reject':'muted'">
        <small>渐近结论（χ²，df {{result.df}}）</small>
        <strong>{{pvalue(result.p_asymptotic)}}</strong>
        <span>{{result.omnibus_reject?'在预设 α='+result.alpha+' 下拒绝':'α='+result.alpha+' 下未拒绝，不证明分布相同'}}</span>
      </div>
      <div class="perm-conclusion" :class="done&&result.permutation_reject?'reject':'muted'">
        <small>置换结论（{{isMc?'抽样 +1 约定':'穷举 exact'}}）</small>
        <strong>{{done?pvalue(permutation.p_value as number):'计算中…'}}</strong>
        <span v-if="done">{{result.permutation_reject?'在预设 α='+result.alpha+' 下拒绝':'α='+result.alpha+' 下未拒绝，不证明分布相同'}}</span>
        <span v-else>进度完成后按同一预设 α 判定</span>
      </div>
    </div>

    <div v-if="isMc" class="method-note">
      <b>抽样误差必须随结论一起报告</b>
      <p>二项命中标准误 SE = {{done?num(permutation.standard_error as number,6):'—'}}；正态近似 95% 置信区间：
        {{done?num(permutation.ci95_low as number,6):'—'}} ～ {{done?num(permutation.ci95_high as number,6):'—'}}。</p>
      <p>p 值采用 +1 约定 (extreme+1)/(draws+1)（Phipson &amp; Smyth 2010，与 SciPy permutation_test 一致）；固定种子的抽签序列连同 RNG 状态持久化，可独立逐次复算。</p>
      <p class="muted">{{permutation.error_basis}}</p>
    </div>
    <div v-else class="method-note">
      <b>穷举无抽样误差</b>
      <p>全部 {{permutation.target_allocations.toLocaleString('zh-CN')}} 种保持组大小的分配均按确定词典序枚举，p = 极端分配数 / 全部分配数，标准误为 0；枚举顺序确定，跳过已完成序号即可无损恢复。</p>
      <p class="muted">{{permutation.p_value_rule}}</p>
    </div>
    <p class="muted">置换检验与渐近检验回答同一总体分布零假设，但数值方法不同、结论可能不一致；两者并列保留，不互相覆盖。等级有序非等距，拒绝仍可能来自分布形状而非纯位置差。</p>
  </section>
</template>
