import pandas as pd, numpy as np, json
pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
P=pd.read_csv('/home/claude/kursrutsch-scanner/backtest/daten/faktor_etfs.csv.gz',index_col=0,parse_dates=True)
# Datenqualität: größte Tagessprünge
r=P.pct_change(fill_method=None)
print(r.abs().max().round(3).sort_values(ascending=False).head(8))
von,bis='2016-10-28','2026-10-28'
M=P.loc[von:bis].ffill().resample('ME').last()
M.iloc[0]=P.loc[:von].ffill().iloc[-1]
def stat(s):
    s=s.dropna(); j=(s.index[-1]-s.index[0]).days/365.25
    mr=s.pct_change().dropna(); dd=(s/s.cummax()-1).min()
    return dict(cagr=(s.iloc[-1]/s.iloc[0])**(1/j)-1, vola=mr.std()*np.sqrt(12), maxdd=dd, ende=10000*s.iloc[-1]/s.iloc[0])
eu=['EUNL.DE','IS3R.DE','IS3Q.DE','IS3S.DE','IQQ0.DE','XDEM.DE','XDEQ.DE','XDEV.DE','XDEB.DE','IS3N.DE']
T=pd.DataFrame({t:stat(M[t]) for t in eu}).T
T['rendite/vola']=T.cagr/T.vola
print(T.round(3))
# Jahresrenditen
J=M[eu].resample('YE').last(); J=pd.concat([M[eu].iloc[[0]],J]).pct_change().dropna(); J.index=J.index.year
print(J.round(3))
# Rollierende 3-Jahres-Mehrrendite ggü. World
R3=(M[eu]/M[eu].shift(36))**(1/3)-1
print('Anteil 3J-Fenster besser als World:'); print((R3.sub(R3['EUNL.DE'],axis=0)>0)[R3['EUNL.DE'].notna()].mean().round(2))
def port(gew,reb='YE'):
    ret=M[list(gew)].pct_change().fillna(0); w=pd.Series(gew,dtype=float); val=10000.0; hold=w*val; out=[]
    for d,row in ret.iterrows():
        hold=hold*(1+row[list(gew)]); val=hold.sum(); out.append(val)
        if d.month==12: hold=w*val
    return pd.Series(out,index=M.index)
pf={'100 % Welt':{'EUNL.DE':1},
 'Welt 70 / M-Q-V je 10':{'EUNL.DE':.7,'IS3R.DE':.1,'IS3Q.DE':.1,'IS3S.DE':.1},
 'Welt 50 / M-Q-V je 16,7':{'EUNL.DE':.5,'IS3R.DE':1/6,'IS3Q.DE':1/6,'IS3S.DE':1/6},
 'M-Q-V je 1/3':{'IS3R.DE':1/3,'IS3Q.DE':1/3,'IS3S.DE':1/3},
 'Momentum 50 / Qualität 50':{'IS3R.DE':.5,'IS3Q.DE':.5},
 'Welt 70 / Momentum 15 / Qualität 15':{'EUNL.DE':.7,'IS3R.DE':.15,'IS3Q.DE':.15},
 'Welt 80 / EM 20':{'EUNL.DE':.8,'IS3N.DE':.2},
 'Welt 60 / EM 10 / M 15 / Q 15':{'EUNL.DE':.6,'IS3N.DE':.1,'IS3R.DE':.15,'IS3Q.DE':.15},
}
PF=pd.DataFrame({k:port(v) for k,v in pf.items()})
S=pd.DataFrame({k:stat(PF[k]) for k in PF}).T; S['rendite/vola']=S.cagr/S.vola
print(S.round(3))
# Teilzeiträume
for a,b in [('2016-10-28','2021-10-31'),('2021-10-31','2026-10-28')]:
    x=PF.loc[a:b]; print(a,b,((x.iloc[-1]/x.iloc[0])**(1/5)-1).round(3).to_dict())
PF.to_pickle('pf.pkl'); M.to_pickle('mon.pkl'); T.to_pickle('T.pkl'); J.to_pickle('J.pkl'); S.to_pickle('S.pkl')
