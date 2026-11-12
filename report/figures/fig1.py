import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
plt.rcParams['font.family']='Liberation Sans'
W,H=10.0,9.0
fig=plt.figure(figsize=(3.4,3.4*H/W)); ax=fig.add_axes([0,0,1,1]); ax.set_xlim(0,W); ax.set_ylim(0,H); ax.axis('off')
INK='#1a1a1a'; DATA='#1f5fa8'; CTRL='#b0461f'
def box(x0,y0,x1,y1,fc,ec=INK):
    ax.add_patch(FancyBboxPatch((x0,y0),x1-x0,y1-y0,boxstyle='round,pad=0,rounding_size=0.12',fc=fc,ec=ec,lw=0.8))
def arrow(a,b,c,ls='-',rad=0):
    ax.add_patch(FancyArrowPatch(a,b,arrowstyle='-|>',mutation_scale=7,lw=1.0,color=c,ls=ls,shrinkA=0,shrinkB=0,connectionstyle=f'arc3,rad={rad}'))
def t(x,y,s,size=6.6,**k):
    k.setdefault('ha','center'); k.setdefault('va','center'); k.setdefault('color',INK)
    ax.text(x,y,s,fontsize=size,**k)
# controller
box(3.3,7.55,9.9,8.85,'#fbe9df')
t(6.6,8.42,'Controller',7.2,weight='bold'); t(6.6,7.95,'reads load, moves buckets')
# load balancer
box(3.3,4.75,8.3,6.45,'#e3edf8')
t(5.8,6.08,'Load balancer',7.2,weight='bold'); t(5.8,5.62,'director (eBPF at TC)'); t(5.8,5.15,'4096 buckets: first, second server')
# client
box(0.1,5.0,2.0,6.2,'#f1f1f1'); t(1.05,5.6,'Client',7.2,weight='bold')
# servers
xs=[0.1,2.65,5.2,7.75]; w=2.15
for i,x in enumerate(xs):
    box(x,0.95,x+w,2.75,'#eef5ea')
    t(x+w/2,2.38,f'Server {i+1}',7.0,weight='bold'); t(x+w/2,1.9,'redirector (TC)',6.2); t(x+w/2,1.42,'+ application',6.2)
# data path
arrow((2.0,5.6),(3.3,5.6),DATA); t(2.65,5.9,'TCP',6.2,color=DATA)
arrow((3.9,4.75),(1.75,2.75),DATA); t(3.2,3.75,'1. UDP encap to\nthe first server',6.2,color=DATA,ha='left')
arrow((2.25,1.85),(2.65,1.85),DATA,ls=(0,(2,1.2)))
t(0.1,0.42,'2. no socket for an old connection: forward once to the second server',6.2,color=DATA,ha='left')
arrow((0.55,2.75),(0.55,5.0),DATA); t(0.72,3.95,'3. reply,\ndirect',6.2,color=DATA,ha='left')
# control path
arrow((5.8,7.55),(5.8,6.45),CTRL); t(5.98,7.0,'new table,\nswapped in at once',6.2,color=CTRL,ha='left')
arrow((9.35,2.75),(9.35,7.55),CTRL,ls=(0,(2,1.2)))
t(9.02,5.15,'load signal from every server,\nevery 1–2 s',6.2,color=CTRL,rotation=90)
fig.savefig('fig1.png',dpi=400,facecolor='white')
