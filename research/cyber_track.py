import json,csv,sys,re
D=sys.argv[1]  # YYYY-MM-DD
CSV='/workspace/tw-stock/research/資安族群追蹤.csv'
rows=list(csv.DictReader(open(CSV,encoding='utf-8')))
base={r['代號']:(r['組別'],r['名稱'],r['市場'],float(r['基準收盤10/29'])) for r in rows if r['日期']=='2026-10-30'}
prev={}
for r in rows:
    if r['市場'] in('上市','上櫃') and r['日期']<D: prev[r['代號']]=float(r['收盤'])
tw=json.load(open('/tmp/twse.json'));tp=json.load(open('/tmp/tpex.json'));ti=json.load(open('/tmp/tpi.json'))
q={}
for r in tw['tables'][8]['data']:
    try:q[r[0].strip()]=float(r[8].replace(',',''))
    except:pass
for r in tp['tables'][0]['data']:
    try:q[r[0].strip()]=float(r[2].replace(',',''))
    except:pass
for r in tw['tables'][0]['data']:
    if r[0]=='發行量加權股價指數':q['TAIEX']=float(r[1].replace(',',''))
q['TPEX']=float(ti['tables'][0]['data'][-1][4])
out=[];rows=[r for r in rows if r['日期']!=D]
for c,(g,n,m,b) in base.items():
    if c not in q: print('MISSING',c);continue
    cl=q[c];p=prev[c];ch=round(cl-p,2)
    r=dict(zip(['日期','組別','代號','名稱','市場','收盤','漲跌','當日漲跌幅%','累計漲跌幅%(vs10/29)','基準收盤10/29'],[D,g,c,n,m,cl,ch,round(ch/p*100,2),round((cl/b-1)*100,2),b]))
    out.append(r)
w=csv.DictWriter(open(CSV,'w',encoding='utf-8',newline=''),fieldnames=list(rows[0].keys()));w.writeheader();w.writerows(rows+out)
for r in out:print(r['組別'],r['代號'],r['名稱'],r['收盤'],r['當日漲跌幅%'],r['累計漲跌幅%(vs10/29)'])
