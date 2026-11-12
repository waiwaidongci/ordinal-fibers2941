"""Reproducible permutation test for the omnibus Kruskal–Wallis statistic.

设计约束（见 README「置换检验与可复算进度」）：
- 零假设下各组分布相同；对全部 N 个合并平均秩重新分配，保持每组试样数 n_g 不变，
  每次分配重新计算并列修正后的同一统计量 H（与渐近总体检验完全相同的公式）。
- 可复算：穷举模式按确定的词典序枚举；抽样模式由固定种子的 NumPy Generator 决定，
  RNG bit_generator state 随检查点持久化，中断恢复后继续同一序列，不重复计数。
- 分配数不超过 exhaustive_limit 时穷举全部 C(N; n_1..n_k) 种分配（含观察分配）；
  否则按 random_draws 次带预设种子的随机分配抽样，并报告 Monte Carlo 误差。
- 每批处理原子提交一次检查点（已完成数/命中数/RNG 状态）；崩溃只丢当前未提交批，
  恢复后从检查点继续，结果与一次不中断运行一致。
"""
import math
from itertools import combinations
import numpy as np

# 可通过环境变量覆盖（src.config.settings 读取）；默认值在此兜底。
DEFAULT_SEED = 20261112
DEFAULT_EXHAUSTIVE_LIMIT = 100_000
DEFAULT_RANDOM_DRAWS = 20_000
DEFAULT_BATCH_SIZE = 500
# 正态近似 95% 置信区间分位数。
_Z95 = 1.959963984540054
_TIE_TOL = 1e-12


def count_allocations(n, group_sizes):
    """多元组合数 C(n; n_1..n_k)，保持各组试样数的不同分配总数。"""
    total = 1
    used = 0
    for size in group_sizes:
        size = int(size)
        total *= math.comb(n - used, size)
        used += size
    return total


def plan_permutation(group_sizes, exhaustive_limit=DEFAULT_EXHAUSTIVE_LIMIT):
    """决定穷举或抽样，并返回计划元数据。"""
    n = int(sum(group_sizes))
    exact_total = count_allocations(n, group_sizes)
    log_total = math.lgamma(n + 1) - sum(math.lgamma(int(s) + 1) for s in group_sizes)
    mode = 'exhaustive' if exact_total <= int(exhaustive_limit) else 'monte_carlo'
    return {
        'mode': mode,
        'n_allocations': int(exact_total),
        'log_n_allocations': float(log_total),
        'group_sizes': [int(s) for s in group_sizes],
    }


def initial_progress(plan, seed, random_draws, batch_size):
    """构造写入 result.permutation 的初始进度块。"""
    target = int(plan['n_allocations']) if plan['mode'] == 'exhaustive' else int(random_draws)
    return {
        'mode': plan['mode'],
        'seed': int(seed),
        'status': 'running',
        'n_allocations': int(plan['n_allocations']),
        'log_n_allocations': float(plan['log_n_allocations']),
        'group_sizes': list(plan['group_sizes']),
        'target_allocations': target,
        'completed_allocations': 0,
        'extreme_count': 0,
        'batch_size': int(batch_size),
        'rng_state': None,
        'observed_h': None,
        'p_value': None,
        'standard_error': None,
        'ci95_low': None,
        'ci95_high': None,
        'resume_count': 0,
        'p_value_rule': None,
        'error_basis': None,
    }


def _h_constants(ranks, group_sizes):
    """H 统计量对任意一次秩分配保持不变的常量；C 仅由等级并列结构决定。"""
    n = int(len(ranks))
    offset = np.concatenate(([0], np.cumsum(group_sizes))).astype(int)
    sizes = np.asarray(group_sizes, dtype=float)
    _, counts = np.unique(ranks, return_counts=True)
    tie_term = int(sum(int(t) ** 3 - int(t) for t in counts))
    denom = n ** 3 - n
    correction = (denom - tie_term) / denom
    return {
        'n': n,
        'k': len(group_sizes),
        'offset': offset,
        'sizes': sizes,
        'coeff': 12.0 / (n * (n + 1)),
        'center': (n + 1) / 2.0,
        'correction': float(correction),
    }


def _h_batch(perm_ranks, cst):
    """对一批（每行一次分配）秩排列计算并列修正 H；行内已按组切片顺序排列。"""
    k, offset = cst['k'], cst['offset']
    sums = np.empty((perm_ranks.shape[0], k), dtype=float)
    for g in range(k):
        sums[:, g] = perm_ranks[:, offset[g]:offset[g + 1]].sum(axis=1)
    mean_ranks = sums / cst['sizes']
    h_raw = cst['coeff'] * ((cst['sizes'] * (mean_ranks - cst['center']) ** 2).sum(axis=1))
    return h_raw / cst['correction']


def _enumerate_orders(n, group_sizes):
    """按词典序枚举保持组大小的全部索引划分，逐行 yield 长度 n 的索引数组。

    前 k-1 组由升序组合确定，末组取剩余索引；每种划分恰好出现一次，顺序完全确定，
    因此「跳过前 skip 个」即可从任意检查点恢复，且与不中断运行逐行一致。
    """
    k = len(group_sizes)
    seq = np.arange(n)

    def recurse(g, remaining):
        if g == k - 1:
            yield [remaining]
            return
        size = int(group_sizes[g])
        for combo in combinations(remaining.tolist(), size):
            chosen = np.asarray(combo, dtype=int)
            rest = np.setdiff1d(remaining, chosen, assume_unique=False)
            for tail in recurse(g + 1, rest):
                yield [chosen] + tail

    for parts in recurse(0, seq):
        yield np.concatenate(parts)


def run_permutation(ranks, group_sizes, observed_h, progress, settings, checkpoint_writer):
    """执行（或从检查点恢复）置换检验，通过 checkpoint_writer 原子落盘进度。

    参数
    ----
    ranks: 全部 N 个合并平均秩。
    group_sizes: 各组试样数，顺序与排秩结果一致。
    observed_h: 观察数据的并列修正 H。
    progress: 上一次检查点（首次为 initial_progress 的结果）；就地更新并返回。
    settings: dict，可含 seed/random_draws/batch_size。
    checkpoint_writer: 回调，接收最新 progress dict，负责一次原子提交（DB + 工件）。
    """
    seed = int(settings.get('seed', DEFAULT_SEED))
    random_draws = int(settings.get('random_draws', DEFAULT_RANDOM_DRAWS))
    batch_size = int(settings.get('batch_size', DEFAULT_BATCH_SIZE))
    ranks = np.asarray(ranks, dtype=float)
    group_sizes = [int(s) for s in group_sizes]
    n = len(ranks)
    cst = _h_constants(ranks, group_sizes)
    target = int(progress['target_allocations'])
    completed = int(progress['completed_allocations'])
    extreme = int(progress['extreme_count'])
    observed_h = float(observed_h)
    progress['observed_h'] = observed_h

    if progress['mode'] == 'exhaustive':
        orders = _enumerate_orders(n, group_sizes)
        for _ in range(completed):  # 确定性跳过已提交枚举
            next(orders)
        batch_rows = []
        for order in orders:
            batch_rows.append(ranks[order])
            if len(batch_rows) >= batch_size:
                hvals = _h_batch(np.asarray(batch_rows, dtype=float), cst)
                extreme += int(np.count_nonzero(hvals >= observed_h - _TIE_TOL))
                completed += len(batch_rows)
                batch_rows.clear()
                progress.update(completed_allocations=completed, extreme_count=extreme)
                checkpoint_writer(progress)
        if batch_rows:
            hvals = _h_batch(np.asarray(batch_rows, dtype=float), cst)
            extreme += int(np.count_nonzero(hvals >= observed_h - _TIE_TOL))
            completed += len(batch_rows)
        # 穷举 p = 极端（含等于）分配数 / 全部分配数；观察分配必在枚举内。
        p_value = extreme / target
        progress.update(
            completed_allocations=completed,
            extreme_count=extreme,
            p_value=float(p_value),
            p_value_rule='exact: count(H_perm >= H_obs, ties included) / total allocations',
            standard_error=0.0,
            error_basis='exact enumeration of every size-preserving allocation; no sampling error',
            ci95_low=float(p_value),
            ci95_high=float(p_value),
            status='completed',
        )
        checkpoint_writer(progress)
        return progress

    # Monte Carlo：固定种子的 RNG，状态随检查点持久化。
    rng = np.random.default_rng(seed)
    if progress.get('rng_state') is not None:
        rng.bit_generator.state = progress['rng_state']
    else:
        progress['rng_state'] = rng.bit_generator.state  # 记录初始状态，供独立复算
    remaining = target - completed
    while remaining > 0:
        b = min(batch_size, remaining)
        # 每行一次独立的组大小保持分配：对索引排列后按固定组界切片。
        perms = np.empty((b, n), dtype=float)
        for row in range(b):
            perms[row] = ranks[rng.permutation(n)]
        hvals = _h_batch(perms, cst)
        extreme += int(np.count_nonzero(hvals >= observed_h - _TIE_TOL))
        completed += b
        remaining -= b
        # 先取 RNG 状态再提交：恢复时从该状态继续，恰好不重放本批抽签。
        progress['rng_state'] = rng.bit_generator.state
        progress.update(completed_allocations=completed, extreme_count=extreme)
        checkpoint_writer(progress)
    # +1 约定（Phipson & Smyth 2010，与 scipy.stats.permutation_test 一致）：
    # p = (extreme + 1) / (draws + 1)，extreme 含观察统计量自身。
    draws = target
    p_value = (extreme + 1) / (draws + 1)
    hit_rate = extreme / draws
    se = math.sqrt(hit_rate * (1.0 - hit_rate) / draws) if draws else 0.0
    progress.update(
        p_value=float(p_value),
        p_value_rule='monte carlo (+1, Phipson & Smyth 2010): '
                     '(count(H_perm >= H_obs, ties included) + 1) / (draws + 1)',
        standard_error=float(se),
        error_basis='binomial standard error of the extreme-hit rate with normal-approximation '
                    '95% CI; the fixed-seed permutation sequence is exactly re-runnable',
        ci95_low=float(max(0.0, p_value - _Z95 * se)),
        ci95_high=float(min(1.0, p_value + _Z95 * se)),
        status='completed',
    )
    checkpoint_writer(progress)
    return progress
