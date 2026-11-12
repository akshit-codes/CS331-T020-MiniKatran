"""Fig. 2: usable capacity fraction of each table, session S8 (one dot per run).
Reads code/results/campaign-20261102/s8/runs.jsonl. Run: python3 fig2.py"""
import json, os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, '..', '..', 'code', 'results', 'campaign-20261102', 's8', 'runs.jsonl')
DENOM = 6234.0  # sum of the servers' capacities alone (S1)
NAMES = [('equal', 'Equal shares'), ('cores', 'Shares by cores'), ('oracle', 'Oracle'),
         ('ctl-cpu', 'CPU controller'), ('ctl-rif-total', 'Total-RIF controller')]
caps = {}
for line in open(RUNS):
    r = json.loads(line)
    if r['kind'] == 'capacity' and 'policy' in r:
        caps.setdefault(r['policy'], []).append(100 * r['capacity'] / DENOM)
plt.rcParams['font.family'] = 'Liberation Sans'
INK, MUTED = '#1a1a1a', '#6b6b6b'
fig, ax = plt.subplots(figsize=(3.4, 1.75))
for y, (key, name) in enumerate(reversed(NAMES)):
    v = caps[key]; mean = sum(v) / len(v)
    ax.plot(v, [y] * len(v), 'o', ms=4.2, mfc='#4a78b5', mec='white', mew=0.5, alpha=0.9, zorder=3)
    ax.plot([mean, mean], [y - 0.3, y + 0.3], color=INK, lw=1.6, zorder=4)
    ax.text(101.3, y, f'{mean:.1f}%  ({len(v)} runs)', va='center', ha='left', fontsize=6.3, color=INK)
ax.set_yticks(range(len(NAMES))); ax.set_yticklabels([n for _, n in reversed(NAMES)], fontsize=6.6, color=INK)
ax.set_xlim(60, 100); ax.set_ylim(-0.6, len(NAMES) - 0.4)
ax.set_xticks(range(60, 101, 10)); ax.set_xticklabels([f'{t}%' for t in range(60, 101, 10)], fontsize=6.3, color=MUTED)
ax.set_xlabel('usable capacity fraction (dot = one run, bar = mean)', fontsize=6.3, color=MUTED)
ax.grid(axis='x', color='#e3e3e3', lw=0.6); ax.set_axisbelow(True)
for s in ('top', 'right', 'left'): ax.spines[s].set_visible(False)
ax.spines['bottom'].set_color('#b5b5b5'); ax.tick_params(length=0)
fig.subplots_adjust(left=0.29, right=0.76, top=0.97, bottom=0.2)
fig.savefig(os.path.join(HERE, 'fig2.png'), dpi=400, facecolor='white')
