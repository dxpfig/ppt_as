"""Deterministic PPTD acceptance fixture; source data stays human-readable YAML."""
from pathlib import Path
import yaml

HERE=Path(__file__).resolve().parent/'advanced-fixture'
def save(path,value):path.write_text(yaml.safe_dump(value,allow_unicode=True,sort_keys=False),encoding='utf-8')
def text(i,text,bounds,size=24,font='Noto Sans CJK SC',**extra):return {'elementId':i,'elementType':'text','bounds':bounds,'content':{'text':text,'fontFamily':font,'fontSize':size,'color':'#19382E',**extra}}
def page(elements,notes=''):return {'pageType':'content','background':{'type':'solid','color':'#F6F5EF'},'notes':notes,'elements':elements}
entry={'version':'v2','title':'PPTD 高级能力验收','size':[960,540],'customFonts':[{'family':'JetBrains Mono','src':'https://fonts.googleapis.com/css2?family=JetBrains+Mono'}],'theme':{'colors':{'primary':'#166854','accent':'#D79032'},'textStyles':{'body':{'fontFamily':'Noto Sans CJK SC','fontSize':24}}},'pages':[]}
pages=[]
formulas=[r'分式 \(\frac{a+b}{c}\) 与根式 \(\sqrt{x^2+y^2}\)',r'积分 \(\int_0^1 x^2\,dx=\frac{1}{3}\)',r'矩阵 \(\begin{pmatrix}1&2\\3&4\end{pmatrix}\)',r'求和 \(\sum_{k=1}^{n}k=\frac{n(n+1)}{2}\)']
elements=[text('title','公式 · 图标 · 字体嵌入',[44,24,860,54],32)]
for i,f in enumerate(formulas):elements.append(text('formula-'+str(i),f,[44,92+i*68,710,60],24))
for i,name in enumerate(['fas:lightbulb','far:heart','fab:github']):elements.append({'elementId':'icon-'+str(i),'elementType':'icon','iconName':name,'bounds':[820,95+i*94,62,62],'fill':{'type':'solid','color':'$primary'}})
elements.append(text('embedded-font','JetBrains Mono 0123456789',[44,390,720,45],24,'JetBrains Mono'))
elements.append({'elementId':'math-table','elementType':'table','bounds':[44,452,860,60],'columnWidths':[.3,.7],'rowHeights':[1],'style':{'cellStyle':{'fontFamily':'Noto Sans CJK SC','fontSize':20}},'rows':[[{'text':'单元格公式'},{'text':r'\(E=mc^2\)'}]]})
pages.append(page(elements,'Editable OMML, FA7 geometry and actual EOT embedded font resources.'))
chart_cases=[]
for kind in ['bar','line','area']:
 chart_cases.append((kind,['x','a','b'],[['A',2,6],['B',4,4],['C',3,5]],[{'type':kind,'name':'One','encode':{'x':'x','y':'a'}},{'type':kind,'name':'Two','encode':{'x':'x','y':'b'}}],{}))
chart_cases.extend([
 ('scatter',['x','a'],[[1,2],[2,4],[3,3]],[{'type':'scatter','encode':{'x':'x','y':'a'}}],{}),
 ('bubble',['x','a','s'],[[1,2,4],[2,4,9],[3,3,16]],[{'type':'bubble','encode':{'x':'x','y':'a','size':'s'}}],{}),
 ('candlestick',['x','o','h','l','c'],[['Mon',3,9,1,7],['Tue',7,9,1,3],['Wed',4,8,2,6]],[{'type':'candlestick','encode':{'x':'x','open':'o','high':'h','low':'l','close':'c'}}],{}),
 ('pie',['x','a'],[['A',2],['B',4],['C',3]],[{'type':'pie','encode':{'category':'x','value':'a'}}],{}),
 ('radar',['x','a','b'],[['A',2,3],['B',4,2],['C',3,4],['D',5,3]],[{'type':'radar','name':'One','encode':{'category':'x','y':'a'}},{'type':'radar','name':'Two','encode':{'category':'x','y':'b'}}],{}),
 ('waterfall',['x','a','total'],[['Start',10,True],['Gain',6,False],['Loss',-3,False],['End',13,True]],[{'type':'waterfall','encode':{'x':'x','y':'a','isTotal':'total'}}],{}),
 ('heatmap',['x','y','v'],[['A','One',-2],['A','Two',4],['B','One',3],['B','Two',None],['C','One',5],['C','Two',1]],[{'type':'heatmap','encode':{'x':'x','y':'y','value':'v'},'colorScale':{'type':'diverging'}}],{}),
 ('treemap',['name','v','parent'],[['Root',10,None],['One',4,'Root'],['Two',6,'Root']],[{'type':'treemap','encode':{'category':'name','value':'v','parent':'parent'}}],{}),
 ('sunburst',['name','v','parent'],[['Root',10,None],['One',4,'Root'],['Two',6,'Root']],[{'type':'sunburst','encode':{'category':'name','value':'v','parent':'parent'}}],{}),
 ('sankey',['s','t','v'],[['A','B',3],['A','C',5],['B','D',3],['C','D',5]],[{'type':'sankey','encode':{'source':'s','target':'t','flow':'v'}}],{}),
 ('combo',['x','a','b'],[['A',2,60],['B',4,40],['C',3,50]],[{'type':'bar','name':'One','encode':{'x':'x','y':'a'}},{'type':'line','name':'Two','encode':{'x':'x','y':'b'},'yAxisIndex':1}],{'yAxis':[{'min':0},{'min':0,'max':100}]}),
 ('stream',['x','a','b'],[['A',2,6],['B',4,4],['C',3,5]],[{'type':'area','name':'One','encode':{'x':'x','y':'a'},'stack':'stream'},{'type':'area','name':'Two','encode':{'x':'x','y':'b'},'stack':'stream'}],{}),
])
for kind,cols,rows,series,options in chart_cases:
 elements=[text('title',kind+' · 数据与对象可编辑',[44,24,860,50],30),{'elementId':'chart-'+kind,'elementType':'chart','bounds':[44,94,860,400],'fontFamily':'Noto Sans CJK SC','data':{'cols':cols,'rows':rows},'series':series,'legend':{'position':'bottom'},**options}]
 pages.append(page(elements,'Native chart when equivalent; otherwise editable vector with retained original tabular data.'))
effects=['appear','fade-in','fly-in','zoom-in','wipe-in','float-in','peek-in','rise-in','pulse','grow-shrink','spin','teeter','fill-color','transparency','color-pulse','disappear','fade-out','fly-out','zoom-out','wipe-out','float-out','motion-path']
for start in (0,11):
 elements=[text('title','动画 '+str(start+1)+'–'+str(min(start+11,22)),[44,24,860,54],30)];animations=[]
 for i,effect in enumerate(effects[start:start+11]):
  x=44+(i%4)*224;y=104+(i//4)*128;key='effect-'+effect
  elements.extend([{'elementId':key,'elementType':'shape','shapeName':'roundRect','bounds':[x,y,170,58],'fill':{'type':'solid','color':'#166854'}},text('label-'+key,effect,[x,y+64,208,30],18)])
  anim={'elementId':key,'effect':effect,'trigger':'onClick','durationMs':500,'easing':'ease-in-out'}
  if effect in ('fill-color','color-pulse'):anim['color']='#D79032'
  if effect=='transparency':anim['amount']=.35
  if effect=='motion-path':anim['path']='M 0 0 Q 40 -40 80 0'
  if effect in ('fly-in','fly-out','wipe-in','wipe-out','float-in','float-out','peek-in'):anim['direction']='up'
  animations.append(anim)
 p=page(elements,'Click Next for each effect. Reset restores authored state.');p['animations']=animations;pages.append(p)
for i,p in enumerate(pages):
 name=f'pages/{i+1:02d}.page';entry['pages'].append(name);save(HERE/name,p)
save(HERE/'advanced.pptd',entry)
print(f'Wrote {len(pages)} pages')
