"""Independent interval oracle and business scenario groups, hidden from actor."""
from datetime import datetime,date,time,timedelta,timezone
from copy import deepcopy

TZ=timezone(timedelta(hours=8))


def parsed(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(TZ)


def windows(day,cal):
    name=day.isoformat()
    if name in cal['extra_workdays'] or day.weekday()<5 and name not in cal['holidays']:
        for a,b in [(9,12),(13,18)]:yield datetime.combine(day,time(a),TZ),datetime.combine(day,time(b),TZ)


def add(start,minutes,cal):
    current=parsed(start)
    if minutes==0:return current.isoformat(timespec='seconds')
    left=minutes*60;day=current.date()
    for _ in range(400):
        for a,b in windows(day,cal):
            cursor=max(a,current)
            if cursor>=b:continue
            length=int((b-cursor).total_seconds())
            if left<=length:return (cursor+timedelta(seconds=left)).isoformat(timespec='seconds')
            left-=length
        day+=timedelta(days=1)
    raise ValueError('oracle horizon')


def between(start,end,cal):
    a,b=parsed(start),parsed(end);total=0;day=a.date()
    while day<=b.date():
        for x,y in windows(day,cal):
            total+=max(0,int((min(y,b)-max(x,a)).total_seconds()))
        day+=timedelta(days=1)
    return total//60


def scenarios(spec):
    c=spec['calendar'];monday=spec['monday'];holiday=spec['holiday'];extra=spec['extra'];groups=[]
    def dt(day,clock,offset='+08:00'):return day+'T'+clock+offset
    def group(key,title,calls,critical=False):groups.append({'key':key,'title':title,'calls':calls,'critical':critical})
    def ac(s,n,cal=None):
        cal=deepcopy(c if cal is None else cal);return {'fn':'add_work_minutes','args':[s,n,cal],'expected':add(s,n,cal)}
    def bc(s,e,cal=None):
        cal=deepcopy(c if cal is None else cal);return {'fn':'work_minutes_between','args':[s,e,cal],'expected':between(s,e,cal)}
    def invalid(fn,args):return {'fn':fn,'args':args,'error':'ValueError'}
    group('normal','正常工作时段计算',[ac(dt(monday,'09:30:00'),90)],True)
    group('lunch','跨午休不计时',[ac(dt(monday,'11:45:00'),30),ac(dt(monday,'12:00:00'),1)],True)
    group('ends','恰好窗口末端不跳窗',[ac(dt(monday,'09:00:00'),180),ac(dt(monday,'13:00:00'),300)],True)
    group('outside','工作时间外先规范化',[ac(dt(monday,'08:20:00'),30),ac(dt(monday,'18:00:00'),1),ac(dt(monday,'22:37:00'),60)])
    group('holidays','长假日历跳过',[ac(dt(holiday,'09:00:00'),60),ac(dt(holiday,'17:59:00'),480)],True)
    group('extra','调休周末实际计时',[ac(dt(extra,'09:00:00'),480),bc(dt(extra,'09:00:00'),dt(extra,'18:00:00'))],True)
    utc=parsed(dt(monday,'11:45:00')).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    west=parsed(dt(monday,'17:30:00')).astimezone(timezone(timedelta(hours=-5))).isoformat()
    group('timezone','Z和负偏移按北京时间',[ac(utc,30),ac(west,120)],True)
    group('seconds-lunch','秒级跨午休保留余秒',[ac(dt(monday,'11:59:30'),1),ac(dt(monday,'09:00:30'),180)])
    group('seconds-day','秒级跨日保留余秒',[ac(dt(monday,'17:59:30'),1),ac(dt(extra,'17:59:45'),1)])
    group('zero','零分钟不强制跳工作窗口',[ac(dt(holiday,'12:37:21'),0),ac(dt(monday,'12:22:17'),0)],True)
    group('duration-types','拒绝bool浮点字符串时长',[invalid('add_work_minutes',[dt(monday,'09:00:00'),v,c]) for v in [True,1.5,'30',None]])
    group('duration-range','拒绝负数和超预算分钟',[invalid('add_work_minutes',[dt(monday,'09:00:00'),v,c]) for v in [-1,20161]])
    group('bad-time','无时区小数秒非法日期报ValueError',[invalid('add_work_minutes',[s,1,c]) for s in [monday+'T09:00:00',monday+'T09:00:00.001+08:00','not-a-date',None]])
    group('interval','区间仅累计工作分钟',[bc(dt(monday,'08:00:00'),dt(monday,'19:00:00')),bc(dt(monday,'11:45:00'),dt(monday,'13:15:00'))],True)
    group('round-once','跨窗口小数分钟先累计后取整',[bc(dt(monday,'11:59:30'),dt(monday,'13:00:30')),bc(dt(monday,'11:59:40'),dt(monday,'13:00:30'))])
    group('same-instant','不同时区相同瞬间区间为零',[bc(dt(monday,'09:00:00'),dt(monday,'01:00:00','+00:00'))])
    group('reverse','反向区间必须拒绝',[invalid('work_minutes_between',[dt(monday,'10:00:00'),dt(monday,'09:00:00'),c])],True)
    malformed=[None,{}, {'holidays':'2026-01-01','extra_workdays':[]}, {'holidays':['2026-02-30'],'extra_workdays':[]}, {'holidays':[holiday],'extra_workdays':[holiday]}]
    group('bad-calendar','非法日历和冲突日期拒绝',[invalid('add_work_minutes',[dt(monday,'09:00:00'),1,v]) for v in malformed])
    group('long','跨多日计算不丢失工时',[ac(dt(monday,'17:59:30'),1900)])
    reordered={'holidays':list(reversed(c['holidays']))+c['holidays'][:1],'extra_workdays':c['extra_workdays']*2}
    group('order-invariance','日历条目顺序与重复不影响',[ac(dt(holiday,'11:45:30'),541,reordered)])
    group('purity','调用不修改日历输入',[{**ac(dt(holiday,'11:45:30'),541),'assert_pure':True}],True)
    return groups
