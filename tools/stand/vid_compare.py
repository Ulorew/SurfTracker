#!/usr/bin/env python3
"""Сравнение настроек контура ПО КАДРУ, с двумя гейтами.

ЧТО МЕРЯЕТСЯ. Сдвиг кадра между соседними кадрами фазовой корреляцией.
Панорама (равномерный ход) вычитается, остаток - это и есть то, что уводит
и мажет ведомую цель. Остаток раскладывается на медленный (зубцы) и быстрый
(зуд): для съёмки запаздывание лучше колебания, и эти
две части ошибки надо считать врозь.

ГЕЙТ ПЕРВЫЙ: ОКНО ХОДА ИЩЕТСЯ ПО ДАННЫМ. Приложение стартует с разной
задержкой, и жёстко заданное окно 3.6-8.0 с 18 сентября съехало на разгон
и остановку: панорама вышла 2.75 px/кадр вместо 6.9. Величина выглядела
измеренной, а измерялось не то. Окно теперь - самый длинный участок, где
скорость выше половины рабочей, с отступом от краёв.

ГЕЙТ ВТОРОЙ: ДВИЖЕНИЕ В КОМНАТЕ. Кадр делится на четыре угловых окна, и
если их сдвиги расходятся больше чем на 3 px в 10% кадров, прогон
выбрасывается. Без этого гейта в тот же день три прогона из шести раздули
остаток вдвое: мерилось движение в комнате, а не работа контура.

ОБА ГЕЙТА ВЫБРАСЫВАЮТ, А НЕ ПРЕДУПРЕЖДАЮТ. Печать, которую никто не читает,
здесь уже подводила.
"""
import sys, math, subprocess
sys.path.insert(0,"tools/stand")
import numpy as np
from frame_shift import сдвиги
FS=29.8; NEBW=1.5

def окно_хода(dx):
    """самый длинный участок, где |dx| выше половины рабочего уровня"""
    ур=np.median(np.abs(dx)[np.abs(dx)>np.percentile(np.abs(dx),70)])
    идёт=np.abs(dx)>0.5*ур
    лучш=(0,0); i=0
    while i<len(идёт):
        if идёт[i]:
            j=i
            while j<len(идёт) and идёт[j]: j+=1
            if j-i>лучш[1]-лучш[0]: лучш=(i,j)
            i=j
        else: i+=1
    a,b=лучш
    m=int(0.3*FS)                      # отступ от краёв: разгон и остановка
    return a+m, max(a+m+30, b-m)

def четверти(path, a, b, N=256):
    okno=np.outer(np.hanning(N),np.hanning(N)).astype(np.float32)
    ряды=[]
    for x,y in [(40,40),(984,40),(40,424),(984,424)]:
        vf=f"crop={N}:{N}:{x}:{y},format=gray"
        p=subprocess.Popen(["ffmpeg","-v","error","-i",path,"-vf",vf,"-f","rawvideo","-pix_fmt","gray","-"],
                           stdout=subprocess.PIPE,bufsize=10**8)
        prev=None; out=[]
        while True:
            bb=p.stdout.read(N*N)
            if len(bb)<N*N: break
            f=np.frombuffer(bb,np.uint8).reshape(N,N).astype(np.float32)
            F=np.fft.rfft2((f-f.mean())*okno)
            if prev is not None:
                R=F*np.conj(prev); R/=np.abs(R)+1e-9
                c=np.fft.irfft2(R,s=(N,N)); i=np.unravel_index(np.argmax(c),c.shape)
                out.append(i[1]-N if i[1]>N/2 else i[1])
            prev=F
        p.stdout.close(); p.wait(); ряды.append(np.array(out,float))
    n=min(len(q) for q in ряды); M=np.stack([q[:n] for q in ряды])
    bb=min(b,n)
    раз=M[:,a:bb].max(0)-M[:,a:bb].min(0)
    return float((раз>3).mean())

print("ЧИСТАЯ СЕРИЯ, окно хода найдено по данным\n")
итог={"p40":[],"p10":[]}
for п in range(4):
    for имя in ("p40","p10"):
        тег=f"c{п}_{имя}"; путь=f"runs/vid_clean_18.09/{тег}.mp4"
        s=сдвиги(путь); a,b=окно_хода(s[:,1])
        dx=s[a:b,1]; dy=s[a:b,0]; psr=s[a:b,2]
        гр=четверти(путь,a,b)
        пан=dx.mean(); к=abs(пан)*FS/6.30
        m=np.hypot(dx-пан, dy-dy.mean()); ост=m.std(ddof=1)*math.sqrt(2)
        mm=m-m.mean(); w=np.hanning(len(mm))
        p=2*np.abs(np.fft.rfft(mm*w))**2/w.sum()**2; f=np.fft.rfftfreq(len(mm),1/FS)
        нч=math.sqrt(p[(f>0.3)&(f<2)].sum()/NEBW); вч=math.sqrt(p[f>=2].sum()/NEBW)
        год = гр<0.10
        print(f"  {тег}: окно {a/FS:4.1f}-{b/FS:4.1f} с ({b-a:3d} кадров), панорама {пан:6.2f} px, "
              f"{к:4.1f} px/град, разнобой четвертей {гр:4.0%} -> {'годен' if год else 'ВЫБРОШЕН'}")
        if год: итог[имя].append((ост,нч,вч,к))
print()
for имя in ("p40","p10"):
    v=итог[имя]
    if not v: print(f"  {имя}: годных нет"); continue
    o=np.array([x[0] for x in v]); n=np.array([x[1] for x in v])
    вч=np.array([x[2] for x in v]); к=np.mean([x[3] for x in v])
    sd=o.std(ddof=1) if len(o)>1 else 0.0
    print(f"  {имя} ({len(v)} годных): остаток {o.mean():.2f}+-{sd:.2f} px = {o.mean()/к*60:.2f} угл.мин "
          f"| зубцы {n.mean():.2f} | зуд {вч.mean():.2f}")
if итог["p40"] and итог["p10"]:
    o4=np.array([x[0] for x in итог["p40"]]); o1=np.array([x[0] for x in итог["p10"]])
    n4=np.array([x[1] for x in итог["p40"]]); n1=np.array([x[1] for x in итог["p10"]])
    v4=np.array([x[2] for x in итог["p40"]]); v1=np.array([x[2] for x in итог["p10"]])
    print(f"\n  P=1.0 против P=4.0: остаток x{o1.mean()/o4.mean():.2f}, "
          f"зубцы x{n1.mean()/n4.mean():.2f}, зуд x{v1.mean()/v4.mean():.2f}")
    print(f"  разбросы: p40 {o4.min():.2f}..{o4.max():.2f}, p10 {o1.min():.2f}..{o1.max():.2f}")
