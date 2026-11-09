#!/usr/bin/env python3
"""Low-water status for the Old Ground atlas.

Reads USGS river gauges and NOAA Great Lakes water-level stations (both open, no key) and writes
lowwater.json: for each gauge, how low the water is right now against its own record for this
time of year, as a percentile (0 = lowest ever seen for the date, 50 = normal, 100 = highest).

Rivers: today's flow (cfs) against every daily mean from 1991 on within a week either side of today's date.
Lakes:  the Great Lakes have no real tide (a lunar tide of about an inch). What moves the shoreline is
        (1) the seasonal and year-to-year level, scored as the last 30 days' mean against every monthly
        mean for this month since 1991, and (2) wind setdown and seiches, a drop of a foot or more for a
        few hours when a strong wind pushes water off one shore. Setdown = last hour minus 30-day mean.

usage: build.py <data-dir>     (data-dir holds stats.json cache and the previous lowwater.json)
Stdlib only, so it runs on a bare GitHub Actions runner.
"""
import json, os, sys, time, math, datetime as dt, urllib.request, urllib.parse, urllib.error

UA = 'old-ground-lowwater/1.0 (+https://github.com/benf05ter/gold-atlas-maps)'
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else '.'
NOW = dt.datetime.now(dt.timezone.utc)
HIST_START = 1991
STATS_MAX_AGE_DAYS = 30
P = [0, 5, 10, 25, 50, 75, 90, 95, 100]          # quantiles kept per day of year (0 = record low, 100 = record high)
LOG = []


def log(*a):
    s = ' '.join(str(x) for x in a)
    LOG.append(s)
    print(s, flush=True)


def get(url, tries=4, as_json=True, timeout=60):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json, text/plain, */*'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read().decode('utf-8', 'replace')
            return json.loads(b) if as_json else b
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                break
        except Exception as e:  # network, timeout, bad json
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f'{url[:160]}: {last}')


def doy(d):
    """Day of year 0..364 on a no-leap calendar (Feb 29 counts as Feb 28)."""
    m, dd = d.month, d.day
    if m == 2 and dd == 29:
        dd = 28
    return (dt.date(2001, m, dd) - dt.date(2001, 1, 1)).days


def quant(sorted_vals, p):
    n = len(sorted_vals)
    if not n:
        return None
    k = (n - 1) * p / 100.0
    a = math.floor(k)
    b = min(a + 1, n - 1)
    return sorted_vals[a] + (sorted_vals[b] - sorted_vals[a]) * (k - a)


def pct_from_q(v, q):
    """Percentile of v against quantile row q (values at P)."""
    if v is None or not q:
        return None
    if v <= q[0]:
        return 0.0
    if v >= q[-1]:
        return 100.0
    for i in range(len(P) - 1):
        if q[i] <= v <= q[i + 1]:
            span = q[i + 1] - q[i]
            f = (v - q[i]) / span if span else 0.5
            return P[i] + (P[i + 1] - P[i]) * f
    return None


def pct_rank(v, vals):
    if v is None or not vals:
        return None
    below = sum(1 for x in vals if x < v)
    eq = sum(1 for x in vals if x == v)
    return 100.0 * (below + 0.5 * eq) / len(vals)


def band(pct):
    if pct is None:
        return 'na'
    if pct <= 5:
        return 'vlow'
    if pct <= 10:
        return 'low'
    if pct <= 25:
        return 'blow'
    if pct <= 75:
        return 'norm'
    return 'high'


def r1(x, n=1):
    return None if x is None else round(x, n)


# ---------------- USGS rivers ----------------
NWIS = 'https://waterservices.usgs.gov/nwis'
OGC = 'https://api.waterdata.usgs.gov/ogcapi/v0/collections'


def usgs_history(site):
    """{date: daily mean cfs} from HIST_START on. Legacy NWIS first, then the new Water Data API."""
    out = {}
    try:
        t = get(f'{NWIS}/dv/?format=rdb&sites={site}&parameterCd=00060&statCd=00003&startDT={HIST_START}-01-01&siteStatus=all', as_json=False, timeout=120)
        for ln in t.splitlines():
            if not ln.startswith('USGS'):
                continue
            c = ln.split('\t')
            try:
                v = float(c[3])
            except (ValueError, IndexError):
                continue
            if v >= 0:
                out[c[2]] = v
        if out:
            return out
    except Exception as e:
        log('  nwis dv history failed', site, e)
    url = (f'{OGC}/daily/items?f=json&monitoring_location_id=USGS-{site}&parameter_code=00060&statistic_id=00003'
           f'&time={HIST_START}-01-01T00:00:00Z/..&limit=10000&properties=time,value')
    pages = 0
    while url and pages < 20:
        j = get(url, timeout=120)
        for f in j.get('features', []):
            pr = f.get('properties', {})
            try:
                v = float(pr.get('value'))
            except (TypeError, ValueError):
                continue
            if v >= 0:
                out[str(pr.get('time'))[:10]] = v
        url = next((l['href'] for l in j.get('links', []) if l.get('rel') == 'next'), None)
        pages += 1
    return out


def river_stats(hist):
    by = [[] for _ in range(365)]
    for d, v in hist.items():
        try:
            k = doy(dt.date.fromisoformat(d))
        except ValueError:
            continue
        by[k].append(v)
    rows = []
    for k in range(365):
        w = []
        for o in range(-7, 8):
            w += by[(k + o) % 365]
        w.sort()
        rows.append([r1(quant(w, p), 1) for p in P] if len(w) >= 60 else None)
    years = len({d[:4] for d in hist})
    return {'q': rows, 'years': years}


def usgs_recent(ids):
    """{site: {...}} latest flow/stage, 30-day daily means, name, coords."""
    res = {i: {} for i in ids}
    ok = False
    try:
        j = get(f'{NWIS}/iv/?format=json&sites={",".join(ids)}&parameterCd=00060,00065&siteStatus=all', timeout=90)
        for ts in j['value']['timeSeries']:
            si = ts['sourceInfo']
            s = si['siteCode'][0]['value']
            r = res.setdefault(s, {})
            r['n'] = si.get('siteName')
            g = si.get('geoLocation', {}).get('geogLocation', {})
            r['la'], r['lo'] = g.get('latitude'), g.get('longitude')
            code = ts['variable']['variableCode'][0]['value']
            nd = ts['variable'].get('noDataValue')
            vals = [x for x in ts['values'][0]['value'] if x.get('value') not in (None, '') and float(x['value']) != nd and float(x['value']) > -999]
            if vals:
                r['q' if code == '00060' else 'gh'] = float(vals[-1]['value'])
                r['qd' if code == '00060' else 'ghd'] = vals[-1]['dateTime']
        j = get(f'{NWIS}/dv/?format=json&sites={",".join(ids)}&parameterCd=00060&period=P30D&siteStatus=all', timeout=90)
        for ts in j['value']['timeSeries']:
            s = ts['sourceInfo']['siteCode'][0]['value']
            vals = [(x['dateTime'][:10], float(x['value'])) for x in ts['values'][0]['value'] if x.get('value') not in (None, '') and float(x['value']) >= 0]
            if vals:
                res.setdefault(s, {})['dv'] = vals
        ok = True
    except Exception as e:
        log('nwis recent failed, trying the Water Data API:', e)
    if ok:
        return res
    t0 = (NOW - dt.timedelta(days=31)).strftime('%Y-%m-%dT00:00:00Z')
    for s in ids:
        r = res[s]
        try:
            m = get(f'{OGC}/monitoring-locations/items/USGS-{s}?f=json')
            r['n'] = m['properties'].get('monitoring_location_name')
            r['lo'], r['la'] = m['geometry']['coordinates'][:2]
            for code, key in (('00060', 'q'), ('00065', 'gh')):
                j = get(f'{OGC}/latest-continuous/items?f=json&monitoring_location_id=USGS-{s}&parameter_code={code}')
                for f in j.get('features', [])[:1]:
                    pr = f['properties']
                    r[key] = float(pr['value'])
                    r[key + 'd' if key == 'gh' else 'qd'] = pr['time']
            j = get(f'{OGC}/daily/items?f=json&monitoring_location_id=USGS-{s}&parameter_code=00060&statistic_id=00003&time={t0}/..&limit=100&properties=time,value')
            r['dv'] = sorted((str(f['properties']['time'])[:10], float(f['properties']['value'])) for f in j.get('features', []) if f['properties'].get('value') is not None)
        except Exception as e:
            log('  water data api failed', s, e)
    return res


# ---------------- NOAA Great Lakes ----------------
COOPS = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
MD = 'https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations'


def coops(params):
    q = dict(params, units='english', time_zone='gmt', format='json', application='old_ground_atlas')
    j = get(COOPS + '?' + urllib.parse.urlencode(q), timeout=90)
    if 'error' in j:
        raise RuntimeError(j['error'].get('message', 'error'))
    return j


def lake_history(st):
    """({month: [monthly means]}, datum) from HIST_START on."""
    for datum in ('LWD', 'IGLD'):
        by = {m: [] for m in range(1, 13)}
        got = 0
        for y0 in range(HIST_START, NOW.year + 1, 10):
            y1 = min(y0 + 9, NOW.year)
            try:
                j = coops({'product': 'monthly_mean', 'station': st, 'datum': datum,
                           'begin_date': f'{y0}0101', 'end_date': f'{y1}1231'})
            except Exception as e:
                if 'datum' in str(e).lower():
                    break
                continue
            for row in j.get('data', []):
                try:
                    v = float(row.get('MSL'))
                    by[int(row['month'])].append(v)
                    got += 1
                except (TypeError, ValueError, KeyError):
                    pass
        if got:
            return by, datum
    return None, None


def lake_recent(st, datum):
    j = coops({'product': 'water_level', 'station': st, 'datum': datum, 'range': 720})
    pts = []
    for row in j.get('data', []):
        try:
            pts.append((row['t'], float(row['v'])))
        except (TypeError, ValueError, KeyError):
            pass
    return pts


# ---------------- main ----------------
def main():
    sites = json.load(open(os.path.join(HERE, 'sites.json')))
    sp = os.path.join(OUT, 'stats.json')
    try:
        stats = json.load(open(sp))
    except Exception:
        stats = {}
    try:
        prev = json.load(open(os.path.join(OUT, 'lowwater.json')))
    except Exception:
        prev = {}
    prev_since = {a['k']: a.get('since') for a in prev.get('alerts', [])}
    fresh = lambda e: e and (NOW - dt.datetime.fromisoformat(e['made'])).days < STATS_MAX_AGE_DAYS
    out, missing = [], []
    today = doy(NOW.date())

    # rivers
    rids = [r[0] for r in sites['rivers']]
    meta = {r[0]: r for r in sites['rivers']}
    rec = usgs_recent(rids)
    for s in rids:
        key = 'r' + s
        if not fresh(stats.get(key)):
            try:
                h = usgs_history(s)
                if len(h) > 3650:
                    stats[key] = dict(river_stats(h), made=NOW.isoformat())
                    log('stats', s, len(h), 'days')
                else:
                    log('stats', s, 'too short', len(h))
            except Exception as e:
                log('stats failed', s, e)
        r = rec.get(s, {})
        st = stats.get(key)
        dv = r.get('dv') or []
        q = r.get('q')
        if q is None and dv:
            q, r['qd'] = dv[-1][1], dv[-1][0]
        if q is None or not st or not st['q'][today] or r.get('la') is None:
            missing.append(s)
            continue
        row = st['q'][today]
        mx = max(dv, key=lambda x: x[1]) if dv else None
        pct = pct_from_q(q, row)
        out.append({'id': s, 'k': 'r', 'n': r.get('n') or s, 'w': meta[s][1], 'st': meta[s][2],
                    'la': round(float(r['la']), 5), 'lo': round(float(r['lo']), 5),
                    'v': round(q, 1), 'u': 'cfs', 'd': r.get('qd'), 'pct': r1(pct), 'b': band(pct),
                    'q': [row[2], row[4], row[6]], 'rec': [row[0], row[-1]], 'yrs': st.get('years'),
                    'gh': r1(r.get('gh'), 2), 'dv': dv[-1][0] if dv else None,
                    'mx': mx[1] if mx else None, 'mxd': mx[0] if mx else None})

    # lakes
    for stn, lake in sites['lakes']:
        key = 'l' + stn
        if not fresh(stats.get(key)):
            try:
                by, datum = lake_history(stn)
                m = get(f'{MD}/{stn}.json').get('stations', [{}])[0]
                if by:
                    stats[key] = {'by': by, 'datum': datum, 'n': m.get('name'), 'la': m.get('lat'), 'lo': m.get('lng'),
                                  'made': NOW.isoformat()}
                    log('stats', stn, datum, sum(len(v) for v in by.values()), 'months')
                else:
                    log('stats', stn, 'no monthly means')
            except Exception as e:
                log('stats failed', stn, e)
        st = stats.get(key)
        if not st:
            missing.append(stn)
            continue
        try:
            pts = lake_recent(stn, st['datum'])
        except Exception as e:
            log('recent failed', stn, e)
            pts = []
        if len(pts) < 100:
            missing.append(stn)
            continue
        vals = [v for _, v in pts]
        mean30 = sum(vals) / len(vals)
        last1 = sum(vals[-10:]) / len(vals[-10:])
        day = vals[-240:]
        lo24 = min(day)
        hist = st['by'].get(str(NOW.month)) or st['by'].get(NOW.month) or []
        pct = pct_rank(mean30, hist)
        setd = last1 - mean30
        b = band(pct)
        if setd <= -0.5 and b in ('norm', 'blow', 'high', 'na'):
            b = 'low'
        out.append({'id': stn, 'k': 'l', 'n': st.get('n') or stn, 'w': 'Lake ' + lake, 'la': st['la'], 'lo': st['lo'],
                    'v': round(last1, 2), 'u': 'ft', 'datum': st['datum'], 'd': pts[-1][0].replace(' ', 'T') + 'Z',
                    'm30': round(mean30, 2), 'pct': r1(pct), 'b': b, 'set': round(setd, 2),
                    'lo24': round(lo24 - mean30, 2), 'mlow': r1(min(hist), 2) if hist else None,
                    'mmed': r1(quant(sorted(hist), 50), 2) if hist else None, 'yrs': len(hist)})

    # alerts: a gauge at or below the 10th percentile for the date, or a lake set down half a foot or more
    alerts = []
    for s in out:
        ks = []
        if s['pct'] is not None and s['pct'] <= 10:
            ks.append('vlow' if s['pct'] <= 5 else 'low')
        if s['k'] == 'l' and s['set'] <= -0.5:
            ks.append('setdown')
        for kind in ks:
            k = f"{s['id']}:{kind}"
            alerts.append({'k': k, 'id': s['id'], 'kind': kind, 'since': prev_since.get(k) or NOW.isoformat(timespec='minutes')})

    res = {'v': 1, 'asof': NOW.isoformat(timespec='minutes'), 'sites': out, 'alerts': alerts, 'missing': missing,
           'about': 'River pct: today\'s flow against every daily mean since 1991 within a week of this date. '
                    'Lake pct: last 30 days\' mean level against every monthly mean for this month since 1991. '
                    'set = last hour minus 30-day mean (wind setdown or seiche), feet. 0 = lowest on record.'}
    os.makedirs(OUT, exist_ok=True)
    json.dump(res, open(os.path.join(OUT, 'lowwater.json'), 'w'), separators=(',', ':'))
    json.dump(stats, open(sp, 'w'), separators=(',', ':'))
    open(os.path.join(OUT, 'log.txt'), 'w').write('\n'.join(LOG) + '\n')
    log(f'{len(out)} gauges, {len(alerts)} alerts, missing {missing}')
    if not out:
        sys.exit(1)


if __name__ == '__main__':
    main()
