"""Publish shared public issuer/rating/calendar facts; accepts no broker/account input."""
import html, gzip, base64
import argparse, concurrent.futures, datetime as dt, hashlib, json, math, os, pathlib, re, subprocess, tempfile, time, urllib.parse
UTC = dt.timezone.utc
MOEX = 'https://iss.moex.com/iss/securities.json'
CBR = 'https://ratings.cbr.ru/'
FNS = 'https://bo.nalog.gov.ru'
GROUPS = ['Финансовые инструменты','Негосударственные пенсионные фонды','Страховые организации','Прочие организации','Лизинговые компании','Кредитные организации','Микрофинансовые организации','Объекты суверенного кредитного рейтинга']
RATING_KEYS = ['isin','inn','objectType','ratingAction','ratingValue','kraName','releaseDate','releaseUrl']
def now(): return dt.datetime.now(UTC).isoformat().replace('+00:00','Z')
def fresh(stamp, seconds):
    try: return -300 <= (dt.datetime.now(UTC)-dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds() <= seconds
    except (ValueError,TypeError,AttributeError): return False
def get(url, maximum=8*1024*1024, timeout=15):
    # Normal TLS, no redirects or certificate bypass; only fixed public providers below.
    if not any(url.startswith(h) for h in (MOEX,CBR,FNS+'/','https://www.gtlk.ru/')): raise ValueError('Public source not allowed')
    run = subprocess.run(['curl','--fail','--silent','--show-error','--connect-timeout','5','--max-time',str(timeout),'--max-filesize',str(maximum),'-H','Accept: application/json','-H','User-Agent: Bondio-Android/issuer-report',url],capture_output=True,check=True)
    if len(run.stdout)>maximum: raise ValueError('Oversized public response')
    return run.stdout
def load(path, default):
    try: return json.loads(path.read_text())
    except (OSError,ValueError): return default
def save(path, value):
    raw=(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
    if len(raw)>8*1024*1024: raise ValueError('Public bundle exceeds app transport bound')
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix('.tmp');temporary.write_bytes(raw);temporary.replace(path)
def issuers(targets, previous):
    if fresh(previous.get('issuerAsOf'),20*3600) and previous.get('issuerTable'): return previous['issuerTable'],previous['issuerAsOf']
    columns='secid,isin,emitent_id,emitent_inn,emitent_title,primary_boardid'
    rows=[];seen=set();names=[]
    for start in range(0,30000,100):
        query=urllib.parse.urlencode({'iss.meta':'off','engine':'stock','market':'bonds','is_trading':'1','limit':100,'start':start,'securities.columns':columns})
        table=json.loads(get(MOEX+'?'+query))['securities'];names=table['columns'];page=table['data']
        for row in page:
            item=dict(zip(names,row));isin=item.get('isin')
            if isin in targets:
                identity=json.dumps(row,ensure_ascii=False)
                if identity not in seen: rows.append(row);seen.add(identity)
        if len(page)<100: break
    else: raise ValueError('Incomplete MOEX pagination')
    if len({dict(zip(names,r)).get('isin') for r in rows})<len(targets)*.85: raise ValueError('Insufficient issuer coverage')
    return {'columns':names,'data':rows},now()
def ratings(previous):
    if fresh(previous.get('ratingAsOf'),1500) and previous.get('ratingComplete'): return {k:previous[k] for k in ['ratingAsOf','ratingRows','ratingExpectedCount','ratingPages','ratingPagesCollected','ratingComplete']}
    with tempfile.TemporaryDirectory(prefix='bondio-public-cbr-') as tmp:
        cookie=pathlib.Path(tmp)/'cookies';html=subprocess.check_output(['curl','--fail','--silent','--show-error','--connect-timeout','5','--max-time','15','-c',str(cookie),CBR]).decode()
        if re.search(r'id=[\"\x27]captcha[\"\x27]',html): raise ValueError('CBR requires manual verification')
        token=re.search(r'[\"\x27]bitrix_sessid[\"\x27]\s*:\s*[\"\x27]([a-z0-9]+)',html)
        if not token: raise ValueError('CBR session unavailable')
        csrf=[token.group(1)]
        def post(action,fields,retry=True):
            body=[('sessid',csrf[0])]+fields
            raw=subprocess.check_output(['curl','--fail','--silent','--show-error','--connect-timeout','5','--max-time','20','-b',str(cookie),'-c',str(cookie),'-H','Content-Type: application/x-www-form-urlencoded; charset=UTF-8','-H','X-Requested-With: XMLHttpRequest','-H','Referer: '+CBR,'--data-binary','@-',CBR+'bitrix/services/main/ajax.php?c=prr.form&action='+action+'&mode=ajax'],input=urllib.parse.urlencode(body).encode())
            response=json.loads(raw);data=response.get('data')
            errors=response.get('errors',[])
            renewed=next((e.get('customData',{}).get('csrf') for e in errors if isinstance(e,dict)and e.get('code')=='invalid_csrf'),None)
            if retry and isinstance(renewed,str) and re.fullmatch(r'[a-z0-9]+',renewed):
                csrf[0]=renewed;return post(action,fields,False)
            if response.get('status')!='success' or not isinstance(data,dict) or data.get('captchaResult') is False: raise ValueError('CBR search not complete/available')
            return data
        post('searchRating',[('fields[formSearh]','advanced'),('fields[disclaimer]','1')]+[('fields[typeGroup][]',g)for g in GROUPS])
        def page(number): return post('searchRatingNavigation',[('fields[pageSize]','100'),('fields[pageNumber]',str(number)),('fields[sortingField]','objectName'),('fields[sortingDirection]','ascending')])
        first=page(1);pages=int(first['pageCount']);count=int(first['itemCount']);size=int(first['pageSize'])
        if size!=100 or not 1<=pages<=300 or not 1<=count<=20000: raise ValueError('Unexpected CBR registry bounds')
        all_rows=[];page_fingerprints=set()
        for n in range(1,pages+1):
            data=first if n==1 else page(n)
            if int(data['pageNumber'])!=n or int(data['pageCount'])!=pages or int(data['itemCount'])!=count: raise ValueError('Registry changed during pagination')
            rows=data.get('itemList',[])
            if len(rows)!=min(size,count-(n-1)*size): raise ValueError('CBR page truncated')
            fingerprint=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            if fingerprint in page_fingerprints: raise ValueError('Repeated CBR page under different page number')
            page_fingerprints.add(fingerprint)
            # The official registry contains stable duplicate records within a page.
            # Preserve every source slot; completeness uses its itemCount, never a deduplicated count.
            for row in rows: all_rows.append({k:row.get(k,'') for k in RATING_KEYS})
            time.sleep(.05)
        if len(all_rows)!=count: raise ValueError('CBR registry incomplete')
        return {'ratingAsOf':now(),'ratingRows':all_rows,'ratingExpectedCount':count,'ratingPages':pages,'ratingPagesCollected':pages,'ratingComplete':False,'ratingCoverage':'pagination_boundary_under_review'}
def project_financial(org,reports):
    # Preserve every report/correction and every field consumed by the Android parser.
    # Only unused presentation metadata is removed; latest/ambiguous/correction logic stays in Java.
    def pick(value,keys):return {k:value[k] for k in keys if k in value}
    projected=[]
    for report in reports:
        item=pick(report,['published','isCb','period','publishedCorrectionNumber','actualCorrectionNumber','publishedCorrectionDate','hasAz','organizationInfo'])
        item['typeCorrections']=[]
        for entry in report.get('typeCorrections',[]):
            row=pick(entry,['type']);correction=entry.get('correction')
            if isinstance(correction,dict):
                row['correction']=pick(correction,['correctionVersion','periodType','knd','bfoOrganizationInfo'])
                for key,fields in [('balance',['okud','current1600','current1300','current1410','current1510','current1200','current1500']),('financialResult',['okud','current2110','current2400','current2310','current2320'])]:
                    if isinstance(correction.get(key),dict):row['correction'][key]=pick(correction[key],fields)
            item['typeCorrections'].append(row)
        projected.append(item)
    return pick(org,['inn','id','shortName']),projected

def financial(inn,previous):
    old=previous.get(inn)
    if old and fresh(old.get('asOf'),2400): return inn,old
    row={'issuerInn':inn,'source':FNS,'asOf':now()}
    try:
        if inn=='7720261827':
            url='https://www.gtlk.ru/investors/otchetnost2/2025/msfo/GTLK_2025.pdf';raw=get(url,timeout=20);row.update(source=url,sha256=hashlib.sha256(raw).hexdigest())
        else:
            search=json.loads(get(FNS+'/advanced-search/organizations/search?query='+inn,timeout=10))
            ids={r.get('id') for r in search.get('content',[]) if html.unescape(re.sub(r'<[^>]*>','',str(r.get('inn','')))).strip()==inn and isinstance(r.get('id'),int) and r['id']>0}
            if search.get('totalPages',0)>1 or len(ids)!=1: row['error']='issuer_not_found'
            else:
                identifier=next(iter(ids));org=json.loads(get(FNS+'/nbo/organizations/'+str(identifier),timeout=10));reports=json.loads(get(FNS+'/nbo/organizations/'+str(identifier)+'/bfo/',timeout=10))
                if org.get('inn')!=inn or not isinstance(reports,list): raise ValueError('Wrong financial entity')
                org,reports=project_financial(org,reports);row.update(organization=org,reports=reports)
    except (Exception,) as error:
        if old and fresh(old.get('asOf'),3600) and ('organization'in old or 'sha256'in old): return inn,old
        row['error']='source_unavailable'
    return inn,row

def build(root):
    data=load(root/'data/bonds.json',{});targets={r['isin'] for r in data.get('rows',[]) if re.fullmatch(r'(?:RU|SU)[A-Z0-9]{10}',r.get('isin',''))}
    if len(targets)<50: raise ValueError('Catalogue missing')
    previous=load(root/'data/advisor-core.json',{});
    if previous.get('encoding')=='gzip-base64':previous=json.loads(gzip.decompress(base64.b64decode(previous['payload'])));
    table,issuer_at=issuers(targets,previous);core={'schemaVersion':1,'asOf':now(),'issuerSource':MOEX,'issuerAsOf':issuer_at,'issuerTable':table,'ratingSource':CBR,'financials':{}}
    core.update({k:previous[k] for k in ['ratingAsOf','ratingRows','ratingExpectedCount','ratingPages','ratingPagesCollected'] if k in previous});core['ratingComplete']=False;index=table['columns'].index('emitent_inn');inns=sorted({r[index]for r in table['data']if isinstance(r[index],str)and re.fullmatch(r'[0-9]{10}',r[index])})
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=6);jobs={pool.submit(financial,inn,previous.get('financialRaw',{})):inn for inn in inns};core['financialRaw']={}
    try:
        for future in concurrent.futures.as_completed(jobs,timeout=120):
            inn,value=future.result();core['financialRaw'][inn]=value
    except concurrent.futures.TimeoutError: pass
    finally:
        for future in jobs:future.cancel()
        pool.shutdown(wait=False,cancel_futures=True)
    # Preserve only original last-good facts, never refresh their source timestamps.
    for inn in inns:
        if inn not in core['financialRaw'] and inn in previous.get('financialRaw',{}):core['financialRaw'][inn]=previous['financialRaw'][inn]
    calendars={}
    for isin in targets:
        value=load(root/'data/calendars'/(isin+'.json'),None)
        if value and value.get('isin')==isin and isinstance(value.get('coupons'),list)and isinstance(value.get('amortizations'),list):calendars[isin]=value
    if len(calendars)<len(targets)*.85:raise ValueError('Calendar bundle incomplete')
    # Both files stay below the existing client limit; each source retains its own original time.
    if not core['ratingComplete']:core.pop('ratingRows',None)
    pack=lambda value:{'schemaVersion':1,'encoding':'gzip-base64','payload':base64.b64encode(gzip.compress(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),mtime=0)).decode()}
    save(root/'data/advisor-calendars.json',pack({'schemaVersion':1,'asOf':now(),'calendars':calendars}))
    save(root/'data/advisor-core.json',pack(core))
    print(json.dumps({'catalogue':len(targets),'issuers':len(table['data']),'ratings':len(core.get('ratingRows',[])),'ratingPages':core.get('ratingPages',0),'calendars':len(calendars),'financialIssuers':len(inns),'coreBytes':(root/'data/advisor-core.json').stat().st_size,'calendarBytes':(root/'data/advisor-calendars.json').stat().st_size}))
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--mirror-root',type=pathlib.Path,required=True);args=parser.parse_args();build(args.mirror_root.resolve())
