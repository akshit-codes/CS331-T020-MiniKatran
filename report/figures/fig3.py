"""Fig. 3: p99 per 5-s window in the antagonist runs of sessions S9 and S12.
Reads code/results/campaign-20261102/{s9,s12}/runs.jsonl. Run: python3 fig3.py"""
import json, os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..', '..', 'code', 'results', 'campaign-20261102')
PANELS = [('oracle', 'Oracle table'), ('hindsight', 'Hindsight table'),
          ('cpu', 'CPU controller'), ('rif-total', 'Total-RIF controller')]
TOP = 3000.0  # ms; higher values are drawn at the top edge
runs = {}
for s in ('s9', 's12'):
    for line in open(os.path.join(ROOT, s, 'runs.jsonl')):
        r = json.loads(line)
        if r['kind'] == 'scenario' and r['scenario'] == 'antagonist' and not r.get('invalid'):
            runs.setdefault(r['config'], []).append(r['churn']['window_p99_ms'])
plt.rcParams['font.family'] = 'Liberation Sans'
INK, MUTED = '#1a1a1a', '#6b6b6b'
fig, axes = plt.subplots(2, 2, figsize=(3.4, 2.75), sharex=True, sharey=True)
for ax, (key, name) in zip(axes.flat, PANELS):
    ax.axvspan(60, 180, color='#ececec', lw=0)
    ax.axhline(20, color=INK, lw=0.7, ls=(0, (3, 2)))
    for w in runs[key]:
        t = [5 * i + 2.5 for i in range(len(w))]
        ax.plot(t, [min(v, TOP) if v else TOP for v in w], color='#2f62a3', lw=0.8, alpha=0.8)
    ax.set_yscale('log'); ax.set_ylim(6, TOP); ax.set_xlim(0, 240)
    ax.set_title(f'{name} ({len(runs[key])} runs)', fontsize=6.6, color=INK, pad=2.5, loc='left')
    ax.set_yticks([10, 20, 100, 1000]); ax.set_yticklabels(['10', '20', '100', '1000'], fontsize=6, color=MUTED)
    ax.set_xticks([0, 60, 120, 180, 240]); ax.set_xticklabels(['0', '60', '120', '180', '240'], fontsize=6, color=MUTED)
    ax.minorticks_off(); ax.tick_params(length=0)
    ax.grid(axis='y', color='#e3e3e3', lw=0.5); ax.set_axisbelow(True)
    for sp in ('top', 'right', 'left'): ax.spines[sp].set_visible(False)
    ax.spines['bottom'].set_color('#b5b5b5')
axes[0, 0].text(120, 7.2, 'neighbour on', ha='center', fontsize=5.8, color=MUTED)
axes[0, 1].text(236, 23, 'target 20 ms', ha='right', fontsize=5.8, color=INK)
for ax in axes[1]: ax.set_xlabel('time (s)', fontsize=6.3, color=MUTED, labelpad=1.5)
for ax in axes[:, 0]: ax.set_ylabel('p99 per 5 s (ms)', fontsize=6.3, color=MUTED, labelpad=1.5)
fig.subplots_adjust(left=0.13, right=0.965, top=0.94, bottom=0.12, hspace=0.26, wspace=0.16)
fig.savefig(os.path.join(HERE, 'fig3.png'), dpi=400, facecolor='white')
