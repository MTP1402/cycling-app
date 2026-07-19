from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import psycopg2
import psycopg2.extras
import hashlib
import secrets
import os
import json
import tempfile
from datetime import datetime, date, timedelta
from collections import defaultdict
import httpx

app = FastAPI(title="Cycling Coach API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL  = os.environ.get("DATABASE_URL", "")
ANTHROPIC_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
STRAVA_CLIENT_ID    = os.environ.get("STRAVA_CLIENT_ID", "266143")
STRAVA_CLIENT_SECRET= os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_REDIRECT_URI = "https://cycling-app-production.up.railway.app/strava/callback"
STRAVA_AUTH_URL     = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL    = "https://www.strava.com/oauth/token"
security      = HTTPBearer()

ANNUAL_GOAL    = 6500
WEEKLY_TARGET  = 125
YEAR           = 2026

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def init_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, password TEXT NOT NULL,
            token TEXT UNIQUE, created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS rides (
            id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id),
            ride_date DATE, name TEXT, dist_mi FLOAT, duration_h FLOAT,
            avg_power INTEGER, norm_power INTEGER, avg_hr INTEGER, max_hr INTEGER,
            avg_cadence INTEGER, max_cadence INTEGER,
            p5 INTEGER, p15 INTEGER, p30 INTEGER,
            elev_gain_ft FLOAT, ride_type TEXT DEFAULT 'General',
            is_virtual BOOLEAN DEFAULT FALSE, temp_c FLOAT,
            notes TEXT, created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS strava_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id),
            athlete_id BIGINT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at BIGINT,
            last_sync TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS profiles (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id),
            age INTEGER, weight_lbs FLOAT, location TEXT,
            fitness_level TEXT, ftp INTEGER,
            annual_goal_mi INTEGER, other_goals TEXT,
            health_notes TEXT, injuries TEXT,
            heat_tolerance TEXT, medical_clearance BOOLEAN DEFAULT FALSE,
            interview_complete BOOLEAN DEFAULT FALSE,
            raw_interview TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS coaching_notes (
            id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id),
            note TEXT, created_at TIMESTAMP DEFAULT NOW()
        );
        -- Add missing columns if upgrading from old schema
        ALTER TABLE rides ADD COLUMN IF NOT EXISTS elev_gain_ft FLOAT;
        ALTER TABLE rides ADD COLUMN IF NOT EXISTS ride_type TEXT DEFAULT 'General';
        ALTER TABLE rides ADD COLUMN IF NOT EXISTS is_virtual BOOLEAN DEFAULT FALSE;
    """)
    cur.close(); conn.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE token = %s", (credentials.credentials,))
    user = cur.fetchone(); cur.close(); conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user

def classify_ride(dist_mi, duration_h, avg_power, is_virtual):
    """Auto-classify ride type based on metrics."""
    if is_virtual:
        return 'General'
    if dist_mi and dist_mi >= 62:
        return 'Long Ride (100km+)'
    if avg_power and avg_power > 200:
        return 'Threshold'
    if duration_h and duration_h >= 3:
        return 'Aerobic Endurance'
    if dist_mi and dist_mi < 10:
        return 'Recovery/Rehab'
    return 'General'

def parse_fit_bytes(data):
    try:
        import fitparse
        with tempfile.NamedTemporaryFile(suffix='.fit', delete=False) as tmp:
            tmp.write(data); tmp_path = tmp.name
        ff = fitparse.FitFile(tmp_path)
        session = {}; records = []
        for msg in ff.get_messages():
            if msg.name == 'session':
                for f in msg.fields: session[f.name] = f.value
            elif msg.name == 'record':
                r = {}
                for f in msg.fields: r[f.name] = f.value
                records.append(r)
        os.unlink(tmp_path)

        powers   = [r['power']      for r in records if r.get('power')      and r['power'] > 0]
        cadences = [r['cadence']    for r in records if r.get('cadence')]
        alts     = [r['altitude']   for r in records if r.get('altitude')]

        def best_avg(vals, n):
            if not vals or len(vals) < n: return max(vals) if vals else None
            return round(max(sum(vals[i:i+n])/n for i in range(len(vals)-n+1)))

        np_val = None
        if powers and len(powers) > 30:
            smoothed = [sum(powers[max(0,i-29):i+1])/len(powers[max(0,i-29):i+1]) for i in range(len(powers))]
            np_val = round((sum(x**4 for x in smoothed)/len(smoothed))**0.25)

        # Elevation gain from altitude records
        elev_gain_m = 0.0
        if alts and len(alts) > 1:
            for i in range(1, len(alts)):
                diff = alts[i] - alts[i-1]
                if diff > 0:
                    elev_gain_m += diff
        elev_gain_ft = round(elev_gain_m * 3.28084, 0) if elev_gain_m else None

        start   = session.get('start_time')
        dist    = session.get('total_distance')
        elapsed = session.get('total_elapsed_time')
        sport      = str(session.get('sport', '')).lower()
        sub_sport  = str(session.get('sub_sport', '')).lower()
        # Check file_id for manufacturer (Zwift shows as manufacturer=zwift)
        manufacturer = ''
        for msg2 in ff.get_messages('file_id'):
            for f2 in msg2.fields:
                if f2.name == 'manufacturer' and f2.value:
                    manufacturer = str(f2.value).lower()
        is_virtual = (
            'virtual' in sport or 'indoor' in sport
            or 'virtual' in sub_sport
            or 'zwift' in manufacturer
            or 'zwift' in sport
        )

        dist_mi   = round(dist/1609.34, 2) if dist else None
        duration_h = round(elapsed/3600, 2) if elapsed else None
        avg_power_val = session.get('avg_power')
        ride_type = classify_ride(dist_mi, duration_h, avg_power_val, is_virtual)

        return {
            'ride_date':   start.strftime('%Y-%m-%d') if hasattr(start,'strftime') else str(start)[:10],
            'name':        session.get('sport', 'Ride'),
            'dist_mi':     dist_mi,
            'duration_h':  duration_h,
            'avg_power':   avg_power_val,
            'norm_power':  session.get('normalized_power') or np_val,
            'avg_hr':      session.get('avg_heart_rate'),
            'max_hr':      session.get('max_heart_rate'),
            'avg_cadence': session.get('avg_cadence'),
            'max_cadence': max(cadences) if cadences else None,
            'p5':   best_avg(powers, 5),
            'p15':  best_avg(powers, 15),
            'p30':  best_avg(powers, 30),
            'elev_gain_ft': elev_gain_ft,
            'ride_type':   ride_type,
            'is_virtual':  is_virtual,
            'temp_c':      session.get('avg_temperature'),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail="Could not parse FIT file: " + str(e))

def build_full_dashboard(rides, name, annual_goal=None):
    """Build the full Cycling Analytics dashboard matching the local version."""
    import json as _json

    goal = annual_goal or ANNUAL_GOAL
    sorted_rides = sorted(rides, key=lambda r: r['ride_date'] if r.get('ride_date') else date.min)

    def to_date(d):
        if isinstance(d, date): return d
        if isinstance(d, str): return date.fromisoformat(str(d)[:10])
        return None

    def dur_hrs(r):
        return float(r.get('duration_h') or 0)

    total_mi   = sum(float(r.get('dist_mi') or 0) for r in rides)
    total_hrs  = sum(dur_hrs(r) for r in rides)
    total_elev = sum(float(r.get('elev_gain_ft') or 0) for r in rides)
    virt_count = sum(1 for r in rides if r.get('is_virtual'))
    out_count  = len(rides) - virt_count

    # Weekly
    week_start = date(YEAR-1, 12, 29)
    weeks = {}
    for i in range(53):
        ws = week_start + timedelta(weeks=i)
        weeks[ws] = 0.0
    for r in sorted_rides:
        d = to_date(r.get('ride_date'))
        if not d: continue
        mon = d - timedelta(days=d.weekday())
        if mon in weeks:
            weeks[mon] = weeks.get(mon, 0) + float(r.get('dist_mi') or 0)
    week_labels = [k.strftime('%b %d') for k in sorted(weeks)]
    week_miles  = [round(weeks[k], 1) for k in sorted(weeks)]
    week_target = [WEEKLY_TARGET] * len(week_labels)
    cum_actual  = []
    cum_target  = []
    running = 0.0
    for i, m in enumerate(week_miles):
        running += m
        cum_actual.append(round(running, 1))
        cum_target.append(round(goal * (i+1) / 53, 1))

    # Monthly
    mo_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    mo_keys  = [f'{YEAR}-{m:02d}' for m in range(1,13)]
    mo_mi    = defaultdict(float)
    mo_hrs   = defaultdict(float)
    for r in sorted_rides:
        d = to_date(r.get('ride_date'))
        if not d: continue
        mk = d.strftime('%Y-%m')
        mo_mi[mk]  += float(r.get('dist_mi') or 0)
        mo_hrs[mk] += dur_hrs(r)
    month_miles = [round(mo_mi[k], 1) for k in mo_keys]
    month_hours = [round(mo_hrs[k], 1) for k in mo_keys]

    # Ride types
    all_types = ['General','Casual/Social','Aerobic Endurance',
                 'Long Ride (100km+)','Threshold','Hard/Intervals','Recovery/Rehab']
    type_mi  = defaultdict(float)
    type_hrs = defaultdict(float)
    for r in sorted_rides:
        rt = r.get('ride_type') or 'General'
        type_mi[rt]  += float(r.get('dist_mi') or 0)
        type_hrs[rt] += dur_hrs(r)
    rtype_mi_vals  = [round(type_mi[t], 1)  for t in all_types]
    rtype_hrs_vals = [round(type_hrs[t], 1) for t in all_types]

    # Virtual vs outdoor
    virt_mi  = round(sum(float(r.get('dist_mi') or 0) for r in rides if r.get('is_virtual')), 1)
    out_mi   = round(sum(float(r.get('dist_mi') or 0) for r in rides if not r.get('is_virtual')), 1)
    virt_hrs = round(sum(dur_hrs(r) for r in rides if r.get('is_virtual')), 1)
    out_hrs  = round(sum(dur_hrs(r) for r in rides if not r.get('is_virtual')), 1)

    # Per-ride trends
    ride_dates = []
    ride_power = []
    ride_hr    = []
    ride_elev  = []
    for r in sorted_rides:
        d = to_date(r.get('ride_date'))
        if not d: continue
        ride_dates.append(d.strftime('%b %d'))
        ride_power.append(r.get('avg_power'))
        ride_hr.append(r.get('avg_hr'))
        ride_elev.append(float(r.get('elev_gain_ft') or 0))

    # Coaching charts
    coach_rides  = [r for r in sorted_rides if float(r.get('dist_mi') or 0) >= 5]
    coach_dates  = [to_date(r['ride_date']).strftime('%b %d') for r in coach_rides if to_date(r.get('ride_date'))]
    coach_avgpwr = [r.get('avg_power')   for r in coach_rides]
    coach_np     = [r.get('norm_power')  for r in coach_rides]
    coach_avghr  = [r.get('avg_hr')      for r in coach_rides]
    coach_maxhr  = [r.get('max_hr')      for r in coach_rides]
    coach_avgcad = [r.get('avg_cadence') for r in coach_rides]
    coach_maxcad = [r.get('max_cadence') for r in coach_rides]
    coach_p5     = [r.get('p5')  for r in coach_rides]
    coach_p10    = [r.get('p15') for r in coach_rides]
    coach_p20    = [r.get('p30') for r in coach_rides]
    coach_p10_mid = [(int(p10 or 0) - int(p20 or 0)) if p10 and p20 else None for p10,p20 in zip(coach_p10, coach_p20)]
    coach_p5_top  = [(int(p5  or 0) - int(p10 or 0)) if p5  and p10 else None for p5, p10 in zip(coach_p5,  coach_p10)]

    # Progress
    today        = date.today()
    day_of_year  = today.timetuple().tm_yday
    pace_mi      = round(goal * day_of_year / 366, 1)
    pace_diff    = round(total_mi - pace_mi, 1)
    pace_ahead   = pace_diff >= 0
    pct_complete = round(total_mi / goal * 100, 1) if goal else 0
    remaining    = round(goal - total_mi, 1)

    def j(v): return _json.dumps(v)

    # Build JS strings (no f-strings with JS braces)
    # Pace calculations (needed regardless of goal display)
    pace_color = '#27AE60' if pace_ahead else '#E67E22'
    pace_word  = 'ahead of' if pace_ahead else 'behind'
    pace_bg    = '#E8F8F0' if pace_ahead else '#FEF0E8'

    # Build goal-dependent KPI cards
    if goal:
        goal_cards = (
            "<div class='stat-card'><div class='label'>Remaining</div>"
            + "<div class='value'>" + str(round(remaining,1)) + "</div>"
            + "<div class='sub'>miles to " + str(goal) + "</div></div>"
            + "<div class='stat-card " + ("green" if pace_ahead else "orange") + "'>"
            + "<div class='label'>Pace</div>"
            + "<div class='value'>" + str(abs(pace_diff)) + "</div>"
            + "<div class='sub'>miles " + pace_word + " pace</div></div>"
        )
        goal_subtitle = " &nbsp;&middot;&nbsp; Goal: " + str(goal) + " miles"
        goal_progress = (
            "<div class='progress-wrap'>"
            + "<div class='progress-label'>"
            + "<span><strong>" + str(round(total_mi,1)) + " mi</strong> completed</span>"
            + "<span>Goal: <strong>" + str(goal) + " mi</strong></span>"
            + "</div>"
            + "<div class='progress-bar-bg'>"
            + "<div class='progress-bar-fill' style='width:" + str(min(pct_complete,100)) + "%'></div>"
            + "</div></div>"
        )
    else:
        goal_cards = ""
        goal_subtitle = ""
        goal_progress = ""

    js_weekly = (
        "barChart('weeklyBar'," + j(week_labels) + ","
        "[{label:'Miles',data:" + j(week_miles) + ",backgroundColor:BLUE+'CC'},"
        "{label:'Target (" + str(WEEKLY_TARGET) + ")',data:" + j(week_target) + ",type:'line',"
        "borderColor:ORANGE,borderDash:[6,3],borderWidth:2,pointRadius:0,fill:false}]);"
    )
    js_cumul = (
        "lineChart('cumulativeLine'," + j(week_labels) + ","
        "[{label:'Actual',data:" + j(cum_actual) + ",borderColor:BLUE,backgroundColor:BLUE+'20',fill:true},"
        "{label:'Target pace',data:" + j(cum_target) + ",borderColor:ORANGE,borderDash:[6,3],borderWidth:2,pointRadius:0}]);"
    )
    js_mo_mi = "barChart('monthlyBar'," + j(mo_names) + ",[{label:'Miles',data:" + j(month_miles) + ",backgroundColor:BLUE+'CC'}]);"
    js_mo_hr = "barChart('monthlyHours'," + j(mo_names) + ",[{label:'Hours',data:" + j(month_hours) + ",backgroundColor:PURPLE+'CC'}]);"
    js_rt_mi = "barChart('rtypeMiles'," + j(all_types) + ",[{label:'Miles',data:" + j(rtype_mi_vals) + ",backgroundColor:TYPE_COLORS.map(c=>c+'CC')}],{indexAxis:'y'});"
    js_rt_hr = "barChart('rtypeHours'," + j(all_types) + ",[{label:'Hours',data:" + j(rtype_hrs_vals) + ",backgroundColor:TYPE_COLORS.map(c=>c+'CC')}],{indexAxis:'y'});"
    # Virtual vs outdoor — custom HTML dual-axis chart
    max_mi = max(out_mi + virt_mi, 1)
    max_hr = max(out_hrs + virt_hrs, 1)
    pct_om = round(out_mi / max_mi * 100, 1)
    pct_vm = max(round(virt_mi / max_mi * 100, 1), 2)
    pct_oh = round(out_hrs / max_hr * 100, 1)
    pct_vh = max(round(virt_hrs / max_hr * 100, 1), 2)
    # Virtual vs outdoor chart — built as pure HTML, injected server-side
    _om = str(out_mi); _vm = str(virt_mi); _oh = str(out_hrs); _vh = str(virt_hrs)
    _pm = str(round(max_mi/2)); _ph = str(round(max_hr/2))
    _tm = str(round(max_mi)); _th = str(round(max_hr))
    virt_html = (
        "<div style='font-size:11px;color:#555;margin-bottom:8px;'>"
        + "<span style='display:inline-flex;align-items:center;gap:4px;margin-right:12px;'>"
        + "<span style='width:10px;height:10px;border-radius:2px;background:#27AE60;display:inline-block;'></span>Outdoor</span>"
        + "<span style='display:inline-flex;align-items:center;gap:4px;'>"
        + "<span style='width:10px;height:10px;border-radius:2px;background:#9B59B6;display:inline-block;'></span>Virtual</span></div>"
        + "<div style='font-size:10px;color:#999;display:flex;justify-content:space-between;padding-left:84px;margin-bottom:2px;'>"
        + "<span>0</span><span>" + _pm + "</span>"
        + "<span style='color:#1F4E79;font-weight:600;'>Miles</span>"
        + "<span>" + _tm + "</span></div>"
        + "<div style='border-left:1.5px solid #ccc;margin-left:84px;padding:2px 0;'>"
        + "<div style='display:flex;align-items:center;position:relative;height:26px;margin-bottom:5px;'>"
        + "<span style='position:absolute;left:-88px;font-size:11px;color:#555;width:84px;text-align:right;padding-right:6px;'>Outdoor mi</span>"
        + "<div style='height:22px;width:" + str(pct_om) + "%;background:#27AE60CC;border-radius:0 4px 4px 0;display:flex;align-items:center;padding:0 8px;'>"
        + "<span style='font-size:11px;font-weight:600;color:#fff;'>" + _om + "</span></div></div>"
        + "<div style='display:flex;align-items:center;position:relative;height:26px;margin-bottom:5px;'>"
        + "<span style='position:absolute;left:-88px;font-size:11px;color:#555;width:84px;text-align:right;padding-right:6px;'>Virtual mi</span>"
        + "<div style='height:22px;width:" + str(pct_vm) + "%;background:#9B59B6CC;border-radius:0 4px 4px 0;display:flex;align-items:center;padding:0 8px;min-width:38px;'>"
        + "<span style='font-size:11px;font-weight:600;color:#fff;'>" + _vm + "</span></div></div></div>"
        + "<div style='height:10px;'></div>"
        + "<div style='font-size:10px;color:#999;display:flex;justify-content:space-between;padding-left:84px;margin-bottom:2px;'>"
        + "<span>0</span><span>" + _ph + "</span>"
        + "<span style='color:#1F4E79;font-weight:600;'>Hours</span>"
        + "<span>" + _th + "</span></div>"
        + "<div style='border-left:1.5px solid #ccc;margin-left:84px;padding:2px 0;'>"
        + "<div style='display:flex;align-items:center;position:relative;height:26px;margin-bottom:5px;'>"
        + "<span style='position:absolute;left:-88px;font-size:11px;color:#555;width:84px;text-align:right;padding-right:6px;'>Outdoor hr</span>"
        + "<div style='height:22px;width:" + str(pct_oh) + "%;background:#27AE60CC;border-radius:0 4px 4px 0;display:flex;align-items:center;padding:0 8px;'>"
        + "<span style='font-size:11px;font-weight:600;color:#fff;'>" + _oh + "</span></div></div>"
        + "<div style='display:flex;align-items:center;position:relative;height:26px;'>"
        + "<span style='position:absolute;left:-88px;font-size:11px;color:#555;width:84px;text-align:right;padding-right:6px;'>Virtual hr</span>"
        + "<div style='height:22px;width:" + str(pct_vh) + "%;background:#9B59B6CC;border-radius:0 4px 4px 0;display:flex;align-items:center;padding:0 8px;min-width:36px;'>"
        + "<span style='font-size:11px;font-weight:600;color:#fff;'>" + _vh + "</span></div></div></div>"
    )
    js_virt = ""
    js_elev  = "barChart('elevBar'," + j(ride_dates) + ",[{label:'Elev Gain (ft)',data:" + j(ride_elev) + ",backgroundColor:ORANGE+'CC'}]);"
    js_pwr   = "lineChart('powerLine'," + j(ride_dates) + ",[{label:'Avg Power (W)',data:" + j(ride_power) + ",borderColor:RED,backgroundColor:RED+'20',fill:false,spanGaps:true}]);"
    js_hr    = "lineChart('hrLine'," + j(ride_dates) + ",[{label:'Avg HR (bpm)',data:" + j(ride_hr) + ",borderColor:'#E91E63',backgroundColor:'#E91E6320',fill:false,spanGaps:true}]);"

    js_coach_pwr = (
        "lineChart('coachPower'," + j(coach_dates) + ","
        "[{label:'Avg Power (W)',data:" + j(coach_avgpwr) + ",borderColor:BLUE,backgroundColor:BLUE+'20',fill:false,spanGaps:true},"
        "{label:'Norm Power (W)',data:" + j(coach_np) + ",borderColor:'#1a5276',borderDash:[6,3],borderWidth:2,pointRadius:2,fill:false,spanGaps:true}],"
        "{plugins:{tooltip:{mode:'index',intersect:false,itemSort:function(a,b){return b.datasetIndex-a.datasetIndex;}}}});"
    )
    js_coach_hr = (
        "lineChart('coachHR'," + j(coach_dates) + ","
        "[{label:'Avg HR (bpm)',data:" + j(coach_avghr) + ",borderColor:'#E91E63',backgroundColor:'#E91E6320',fill:false,spanGaps:true},"
        "{label:'Max HR (bpm)',data:" + j(coach_maxhr) + ",borderColor:'#880e4f',borderDash:[6,3],borderWidth:2,pointRadius:2,fill:false,spanGaps:true}],"
        "{plugins:{tooltip:{mode:'index',intersect:false,itemSort:function(a,b){return b.datasetIndex-a.datasetIndex;}}}});"
    )
    js_coach_cad = (
        "lineChart('coachCad'," + j(coach_dates) + ","
        "[{label:'Avg Cadence (rpm)',data:" + j(coach_avgcad) + ",borderColor:'#E67E22',backgroundColor:'#E67E2220',fill:false,spanGaps:true},"
        "{label:'Max Cadence (rpm)',data:" + j(coach_maxcad) + ",borderColor:'#784212',borderDash:[6,3],borderWidth:2,pointRadius:2,fill:false,spanGaps:true}],"
        "{plugins:{tooltip:{mode:'index',intersect:false,itemSort:function(a,b){return b.datasetIndex-a.datasetIndex;}}}});"
    )
    js_sprint_p5  = j(coach_p5)
    js_sprint_p10 = j(coach_p10)
    js_sprint_p20 = j(coach_p20)
    js_coach_sprint = (
        "var _p5=" + js_sprint_p5 + ",_p10=" + js_sprint_p10 + ",_p20=" + js_sprint_p20 + ";"
        "barChart('coachSprint'," + j(coach_dates) + ","
        "[{label:'30s base',data:" + j(coach_p20) + ",backgroundColor:'#27AE60CC',stack:'s'},"
        "{label:'15s mid',data:" + j(coach_p10_mid) + ",backgroundColor:'#2E75B6CC',stack:'s'},"
        "{label:'5s burst',data:" + j(coach_p5_top) + ",backgroundColor:'#E67E22CC',stack:'s'}],"
        "{scales:{y:{stacked:true,beginAtZero:true},x:{stacked:true,ticks:{maxRotation:45}}},"
        "plugins:{tooltip:{mode:'index',intersect:false,"
        "itemSort:function(a,b){return b.datasetIndex-a.datasetIndex;},"
        "callbacks:{label:function(ctx){"
        "var i=ctx.dataIndex;"
        "if(ctx.datasetIndex===2)return '5s: '+(_p5[i]||'-')+'W';"
        "if(ctx.datasetIndex===1)return '15s: '+(_p10[i]||'-')+'W';"
        "return '30s: '+(_p20[i]||'-')+'W';}}}}});"
    )

    return (
        "<!DOCTYPE html><html lang='en'><head>"
        + "<meta charset='UTF-8'>"
        + "<meta name='viewport' content='width=device-width,initial-scale=1.0'>"
        + "<title>" + name + "'s Cycling Dashboard " + str(YEAR) + "</title>"
        + "<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'></script>"
        + "<style>"
        + ":root{--blue:#1F4E79;--blue2:#2E75B6;--blue3:#D6E4F0;--green:#27AE60;--orange:#E67E22;--red:#E74C3C;--purple:#9B59B6;--grey:#F5F7FA;--text:#2C3E50;--card:#FFFFFF;}"
        + "*{box-sizing:border-box;margin:0;padding:0;}"
        + "body{font-family:'Segoe UI',Arial,sans-serif;background:var(--grey);color:var(--text);padding:20px;}"
        + "h1{color:var(--blue);font-size:1.6rem;margin-bottom:4px;}"
        + ".subtitle{color:#666;font-size:0.9rem;margin-bottom:20px;}"
        + ".stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px;}"
        + ".stat-card{background:var(--card);border-radius:10px;padding:14px 16px;box-shadow:0 2px 6px rgba(0,0,0,0.07);border-left:4px solid var(--blue2);}"
        + ".stat-card .label{font-size:0.7rem;color:#888;text-transform:uppercase;letter-spacing:.05em;}"
        + ".stat-card .value{font-size:1.5rem;font-weight:700;color:var(--blue);margin:4px 0 2px;}"
        + ".stat-card .sub{font-size:0.75rem;color:#666;}"
        + ".stat-card.green{border-left-color:var(--green);}"
        + ".stat-card.orange{border-left-color:var(--orange);}"
        + ".stat-card.purple{border-left-color:var(--purple);}"
        + ".progress-wrap{background:var(--card);border-radius:10px;padding:14px 18px;box-shadow:0 2px 6px rgba(0,0,0,0.07);margin-bottom:20px;}"
        + ".progress-label{display:flex;justify-content:space-between;font-size:0.82rem;color:#666;margin-bottom:6px;}"
        + ".progress-bar-bg{background:#E0E0E0;border-radius:8px;height:16px;overflow:hidden;}"
        + ".progress-bar-fill{height:100%;border-radius:8px;background:linear-gradient(90deg,var(--blue2),var(--green));}"
        + ".charts-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:16px;}"
        + ".chart-card{background:var(--card);border-radius:10px;padding:16px 18px;box-shadow:0 2px 6px rgba(0,0,0,0.07);}"
        + ".chart-card h3{font-size:0.88rem;color:var(--blue);margin-bottom:12px;font-weight:600;}"
        + ".chart-card canvas{max-height:260px;}"
        + ".section-header{margin:24px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--blue3);}"
        + ".section-header h2{color:var(--blue);font-size:1rem;font-weight:700;}"
        + ".section-header p{font-size:0.75rem;color:#888;margin-top:3px;}"
        + ".footer{text-align:center;color:#aaa;font-size:0.75rem;margin-top:20px;padding-top:12px;border-top:1px solid #eee;}"
        + "@media(max-width:600px){.charts-grid{grid-template-columns:1fr;}.stats-grid{grid-template-columns:repeat(2,1fr);}}"
        + "</style></head><body>"

        + "<h1>&#x1F6B4; " + name + "'s Cycling Dashboard " + str(YEAR) + "</h1>"
        + "<p class='subtitle'>Updated " + date.today().strftime('%B %d, %Y') + " &nbsp;&middot;&nbsp; " + str(len(rides)) + " rides" + goal_subtitle + "</p>"

        + "<div class='stats-grid'>"
        + "<div class='stat-card green'><div class='label'>Year Total</div>"
        + "<div class='value'>" + str(round(total_mi,1)) + "</div>"
        + "<div class='sub'>miles" + (" &nbsp;(" + str(pct_complete) + "% of goal)" if goal else "") + "</div></div>"

        + (goal_cards if goal_cards else "")

        + "<div class='stat-card'><div class='label'>Hours in Saddle</div>"
        + "<div class='value'>" + str(round(total_hrs,1)) + "</div>"
        + "<div class='sub'>hours total</div></div>"

        + "<div class='stat-card'><div class='label'>Total Rides</div>"
        + "<div class='value'>" + str(len(rides)) + "</div>"
        + "<div class='sub'>" + str(virt_count) + " virtual &nbsp;&middot;&nbsp; " + str(out_count) + " outdoor</div></div>"

        + "<div class='stat-card purple'><div class='label'>Elevation</div>"
        + "<div class='value'>" + str(int(total_elev)) + "</div>"
        + "<div class='sub'>feet climbed total</div></div>"
        + "</div>"

        + (goal_progress if goal_progress else "")

        + "<div class='charts-grid'>"
        + "<div class='chart-card'><h3>&#x1F4C5; Weekly Mileage vs " + str(WEEKLY_TARGET) + "-Mile Target</h3><canvas id='weeklyBar'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F4C8; Cumulative Miles vs Annual Target</h3><canvas id='cumulativeLine'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F4C6; Monthly Miles</h3><canvas id='monthlyBar'></canvas></div>"
        + "<div class='chart-card'><h3>&#x23F1; Hours in the Saddle by Month</h3><canvas id='monthlyHours'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F3F7; Ride Type &#x2014; Miles</h3><canvas id='rtypeMiles'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F3F7; Ride Type &#x2014; Hours</h3><canvas id='rtypeHours'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F7E3; Virtual vs Outdoor</h3>" + virt_html + "</div>"
        + "<div class='chart-card'><h3>&#x26F0; Elevation Gain per Ride (ft)</h3><canvas id='elevBar'></canvas></div>"
        + "<div class='chart-card'><h3>&#x26A1; Average Power per Ride (W)</h3><canvas id='powerLine'></canvas></div>"
        + "<div class='chart-card'><h3>&#x2764; Average Heart Rate per Ride (bpm)</h3><canvas id='hrLine'></canvas></div>"
        + "</div>"

        + "<div class='section-header'>"
        + "<h2>&#x1F3C6; Coaching Analytics &#x2014; Power &middot; Heart Rate &middot; Cadence &middot; Sprint Power</h2>"
        + "<p>Solid line = average &nbsp;&middot;&nbsp; Dashed line = max/normalized &nbsp;&middot;&nbsp; All rides &#x2265; 5 miles</p>"
        + "</div>"

        + "<div class='charts-grid'>"
        + "<div class='chart-card'><h3>&#x26A1; Avg Power vs Normalized Power (W)</h3><canvas id='coachPower'></canvas></div>"
        + "<div class='chart-card'><h3>&#x2764;&#xFE0F; Avg HR vs Max HR (bpm)</h3><canvas id='coachHR'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F504; Avg Cadence vs Max Cadence (rpm)</h3><canvas id='coachCad'></canvas></div>"
        + "<div class='chart-card'><h3>&#x1F3CE;&#xFE0F; Sprint Power &#x2014; 5s / 15s / 30s Best (W)</h3><canvas id='coachSprint'></canvas></div>"
        + "</div>"

        + "<p class='footer'>Generated by Cycling Coach &nbsp;&middot;&nbsp; " + date.today().strftime('%Y-%m-%d') + "</p>"

        + "<script>"
        + "const BLUE='#2E75B6',GREEN='#27AE60',ORANGE='#E67E22',RED='#E74C3C',PURPLE='#9B59B6',GREY='#95A5A6';"
        + "const TYPE_COLORS=[GREY,ORANGE,BLUE,GREEN,'#F39C12',RED,GREEN];"
        + "Chart.defaults.font.family=\"'Segoe UI',Arial,sans-serif\";"
        + "Chart.defaults.font.size=11;Chart.defaults.color='#555';"
        + "function barChart(id,labels,datasets,opts){"
        + "opts=opts||{};"
        + "new Chart(document.getElementById(id),{type:'bar',data:{labels:labels,datasets:datasets},"
        + "options:Object.assign({responsive:true,plugins:{legend:{display:datasets.length>1}},"
        + "scales:{y:{beginAtZero:true},x:{ticks:{maxRotation:45}}}},opts)});}"
        + "function lineChart(id,labels,datasets,opts){"
        + "opts=opts||{};"
        + "new Chart(document.getElementById(id),{type:'line',data:{labels:labels,datasets:datasets},"
        + "options:Object.assign({responsive:true,plugins:{legend:{display:datasets.length>1}},"
        + "scales:{y:{beginAtZero:false},x:{ticks:{maxRotation:45}}},"
        + "elements:{point:{radius:2},line:{tension:0.3}}},opts)});}"
        + js_weekly
        + js_cumul
        + js_mo_mi
        + js_mo_hr
        + js_rt_mi
        + js_rt_hr
        + js_elev
        + js_pwr
        + js_hr
        + js_coach_pwr
        + js_coach_hr
        + js_coach_cad
        + js_coach_sprint
        + "</script></body></html>"
    )

async def get_coaching_summary(user, metrics):
    if not ANTHROPIC_KEY:
        return "AI coaching unavailable."
    try:
        conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM rides WHERE user_id=%s ORDER BY ride_date DESC LIMIT 10", (user['id'],))
        recent = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT note FROM coaching_notes WHERE user_id=%s ORDER BY created_at DESC LIMIT 5", (user['id'],))
        notes = [r['note'] for r in cur.fetchall()]
        cur.execute("SELECT * FROM profiles WHERE user_id=%s", (user['id'],))
        profile = cur.fetchone()
        cur.close(); conn.close()

        profile_ctx = ""
        if profile:
            profile_ctx = "RIDER PROFILE:\n"
            if profile.get('age'):          profile_ctx += "- Age: " + str(profile['age']) + "\n"
            if profile.get('weight_lbs'):   profile_ctx += "- Weight: " + str(profile['weight_lbs']) + " lbs\n"
            if profile.get('location'):     profile_ctx += "- Location: " + str(profile['location']) + "\n"
            if profile.get('fitness_level'):profile_ctx += "- Fitness: " + str(profile['fitness_level']) + "\n"
            if profile.get('ftp'):          profile_ctx += "- FTP: " + str(profile['ftp']) + "W\n"
            if profile.get('annual_goal_mi'):profile_ctx += "- Annual goal: " + str(profile['annual_goal_mi']) + " mi\n"
            if profile.get('other_goals'):  profile_ctx += "- Goals: " + str(profile['other_goals']) + "\n"
            if profile.get('health_notes'): profile_ctx += "- Health: " + str(profile['health_notes']) + "\n"
            if profile.get('injuries'):     profile_ctx += "- Injuries: " + str(profile['injuries']) + "\n"
            if profile.get('heat_tolerance'):profile_ctx += "- Heat tolerance: " + str(profile['heat_tolerance']) + "\n"

        recent_ctx = ""
        if recent:
            recent_ctx = "RECENT RIDES (last 10):\n"
            for r in recent[:5]:
                recent_ctx += "- " + str(r.get('ride_date',''))[:10] + ": " + str(r.get('dist_mi','')) + "mi, HR " + str(r.get('avg_hr','')) + ", pwr " + str(r.get('avg_power','')) + "W\n"

        prompt = (
            profile_ctx + "\n"
            + ("PERSONAL NOTES: " + "; ".join(notes) + "\n\n" if notes else "")
            + recent_ctx + "\n"
            + "LATEST RIDE:\n"
            + "- Date: " + str(metrics.get('ride_date','')) + "\n"
            + "- Distance: " + str(metrics.get('dist_mi','')) + " mi\n"
            + "- Avg power: " + str(metrics.get('avg_power','')) + "W, NP: " + str(metrics.get('norm_power','')) + "W\n"
            + "- Avg HR: " + str(metrics.get('avg_hr','')) + " bpm, Max HR: " + str(metrics.get('max_hr','')) + " bpm\n"
            + "- Cadence: " + str(metrics.get('avg_cadence','')) + " rpm\n"
            + "- Elevation: " + str(metrics.get('elev_gain_ft','')) + " ft\n"
            + "- Temp: " + str(metrics.get('temp_c','')) + "C\n\n"
            + "Give a 3-4 sentence coaching assessment personalized to this rider. "
            + "Reference their specific situation — age, recovery status, heat, goals. Be direct and specific. "
            + "If they mentioned recent illness or injury, factor that in. "
            + "End with one actionable recommendation for their next ride."
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
                json={"model":"claude-sonnet-4-6","max_tokens":300,
                      "messages":[{"role":"user","content":prompt}]},
                timeout=30
            )
            return resp.json()['content'][0]['text']
    except Exception as e:
        return "Coaching summary unavailable: " + str(e)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {"status": "Cycling Coach API running"}

@app.post("/register")
def register(email: str = Form(...), name: str = Form(...), password: str = Form(...)):
    conn = get_db(); cur = conn.cursor()
    try:
        token = secrets.token_hex(32)
        cur.execute("INSERT INTO users (email,name,password,token) VALUES (%s,%s,%s,%s) RETURNING id",
                    (email.lower(), name, hash_password(password), token))
        uid = cur.fetchone()[0]; cur.close(); conn.close()
        return {"token": token, "user_id": uid, "name": name}
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Email already registered")

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE email=%s AND password=%s",
                (email.lower(), hash_password(password)))
    user = cur.fetchone(); cur.close(); conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"token": user['token'], "name": user['name'], "user_id": user['id']}

@app.post("/upload")
async def upload_fit(file: UploadFile = File(...), notes: str = Form(default=""),
                     user: dict = Depends(get_current_user)):
    data    = await file.read()
    metrics = parse_fit_bytes(data)
    conn = get_db(); cur = conn.cursor()
    # Deduplication check
    cur.execute("""SELECT id FROM rides WHERE user_id=%s AND ride_date=%s
        AND ABS(COALESCE(dist_mi,0)-%s)<0.5 AND ABS(COALESCE(duration_h,0)-%s)<0.1""",
        (user['id'], metrics['ride_date'], metrics.get('dist_mi') or 0, metrics.get('duration_h') or 0))
    existing = cur.fetchone()
    if existing:
        cur.close(); conn.close()
        return {"ride_id": existing[0], "metrics": metrics, "coaching": "Already in your database.", "duplicate": True}
    cur.execute("""INSERT INTO rides (user_id,ride_date,name,dist_mi,duration_h,
        avg_power,norm_power,avg_hr,max_hr,avg_cadence,max_cadence,
        p5,p15,p30,elev_gain_ft,ride_type,is_virtual,temp_c,notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (user['id'], metrics['ride_date'], metrics.get('name'),
         metrics.get('dist_mi'), metrics.get('duration_h'),
         metrics.get('avg_power'), metrics.get('norm_power'),
         metrics.get('avg_hr'), metrics.get('max_hr'),
         metrics.get('avg_cadence'), metrics.get('max_cadence'),
         metrics.get('p5'), metrics.get('p15'), metrics.get('p30'),
         metrics.get('elev_gain_ft'), metrics.get('ride_type','General'),
         metrics.get('is_virtual', False), metrics.get('temp_c'), notes))
    ride_id = cur.fetchone()[0]; cur.close(); conn.close()
    coaching = await get_coaching_summary(user, metrics)
    return {"ride_id": ride_id, "metrics": metrics, "coaching": coaching}

@app.get("/rides")
def get_rides(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM rides WHERE user_id=%s ORDER BY ride_date DESC LIMIT 200", (user['id'],))
    rides = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return {"rides": rides, "count": len(rides)}

@app.post("/notes")
def add_note(note: str = Form(...), user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO coaching_notes (user_id,note) VALUES (%s,%s)", (user['id'],note))
    cur.close(); conn.close()
    return {"status": "saved"}

@app.get("/profile")
def get_profile(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM profiles WHERE user_id=%s", (user['id'],))
    profile = cur.fetchone(); cur.close(); conn.close()
    return {"profile": dict(profile) if profile else None, "name": user['name']}

@app.post("/profile")
async def save_profile(
    age: str = Form(default=""),
    weight_lbs: str = Form(default=""),
    location: str = Form(default=""),
    fitness_level: str = Form(default=""),
    ftp: str = Form(default=""),
    annual_goal_mi: str = Form(default=""),
    other_goals: str = Form(default=""),
    health_notes: str = Form(default=""),
    injuries: str = Form(default=""),
    heat_tolerance: str = Form(default=""),
    medical_clearance: str = Form(default="false"),
    user: dict = Depends(get_current_user)
):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO profiles (user_id, age, weight_lbs, location, fitness_level, ftp,
            annual_goal_mi, other_goals, health_notes, injuries, heat_tolerance,
            medical_clearance, interview_complete, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            age=EXCLUDED.age, weight_lbs=EXCLUDED.weight_lbs,
            location=EXCLUDED.location, fitness_level=EXCLUDED.fitness_level,
            ftp=EXCLUDED.ftp, annual_goal_mi=EXCLUDED.annual_goal_mi,
            other_goals=EXCLUDED.other_goals, health_notes=EXCLUDED.health_notes,
            injuries=EXCLUDED.injuries, heat_tolerance=EXCLUDED.heat_tolerance,
            medical_clearance=EXCLUDED.medical_clearance,
            interview_complete=true, updated_at=NOW()
    """, (
        user['id'],
        int(age) if age.strip() else None,
        float(weight_lbs) if weight_lbs.strip() else None,
        location or None, fitness_level or None,
        int(ftp) if ftp.strip() else None,
        int(annual_goal_mi) if annual_goal_mi.strip() else None,
        other_goals or None, health_notes or None,
        injuries or None, heat_tolerance or None,
        medical_clearance.lower() == 'true'
    ))
    cur.close(); conn.close()
    return {"status": "saved"}

@app.post("/interview")
async def ai_interview(
    message: str = Form(...),
    history: str = Form(default="[]"),
    user: dict = Depends(get_current_user)
):
    """Conversational AI entrance interview."""
    if not ANTHROPIC_KEY:
        return {"reply": "AI unavailable.", "profile_update": {}}
    
    import json as _json
    
    # Get existing profile and notes
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM profiles WHERE user_id=%s", (user['id'],))
    profile = cur.fetchone()
    cur.execute("SELECT note FROM coaching_notes WHERE user_id=%s ORDER BY created_at DESC LIMIT 5", (user['id'],))
    notes = [r['note'] for r in cur.fetchall()]
    cur.close(); conn.close()

    # Build profile context for system prompt
    profile_ctx = ""
    if profile:
        profile_ctx = "\n\nEXISTING RIDER PROFILE (already on file — do not ask for this again):\n"
        if profile.get('age'):           profile_ctx += "- Age: " + str(profile['age']) + "\n"
        if profile.get('weight_lbs'):    profile_ctx += "- Weight: " + str(profile['weight_lbs']) + " lbs\n"
        if profile.get('location'):      profile_ctx += "- Location: " + str(profile['location']) + "\n"
        if profile.get('fitness_level'): profile_ctx += "- Fitness: " + str(profile['fitness_level']) + "\n"
        if profile.get('ftp'):           profile_ctx += "- FTP: " + str(profile['ftp']) + "W\n"
        if profile.get('annual_goal_mi'):profile_ctx += "- Annual goal: " + str(profile['annual_goal_mi']) + " mi\n"
        if profile.get('other_goals'):   profile_ctx += "- Goals: " + str(profile['other_goals']) + "\n"
        if profile.get('health_notes'):  profile_ctx += "- Health: " + str(profile['health_notes']) + "\n"
        if profile.get('injuries'):      profile_ctx += "- Injuries: " + str(profile['injuries']) + "\n"
        if profile.get('heat_tolerance'):profile_ctx += "- Heat tolerance: " + str(profile['heat_tolerance']) + "\n"
    if notes:
        profile_ctx += "\nPERSONAL NOTES: " + "; ".join(notes) + "\n"

    try:
        hist = _json.loads(history)
    except:
        hist = []

    system_prompt = """You are a friendly cycling coach conducting an ongoing coaching conversation with an athlete.
Your goal is to gather key information naturally through conversation:
- Age and weight
- Where they ride (city/region/climate — heat, altitude, terrain)
- Fitness level and cycling experience  
- FTP if they know it, or riding history
- Primary goals (mileage target, events, fitness, weight loss)
- Any injuries, recent illnesses, or medical conditions
- Heat tolerance and any history of heat-related issues
- Whether they have medical clearance if they mention serious conditions

IMPORTANT RULES:
- If they mention any serious cardiac conditions, recent surgery, chest pain during exercise, or uncontrolled medical conditions: ALWAYS say they should consult their doctor before continuing and ask if they have medical clearance.
- If they mention wanting to lose weight: acknowledge it warmly but note that cycling supports overall health — direct specific dietary advice to a nutritionist.
- If they mention recent COVID, flu, mono, or similar illness: briefly note the post-viral performance dip and adjust expectations.
- Keep responses conversational, warm, 2-4 sentences max.
- After gathering enough info (3-4 exchanges), summarize what you've learned and ask if there's anything else important to share.
- End by saying their profile has been saved and coaching will be personalized to them." + profile_ctx + "

At the END of your response, on a new line, output a JSON object (and ONLY the JSON, no other text on that line) with any profile fields you extracted:
{"age":null,"weight_lbs":null,"location":null,"fitness_level":null,"ftp":null,"annual_goal_mi":null,"other_goals":null,"health_notes":null,"injuries":null,"heat_tolerance":null,"medical_clearance":false}
Only include fields where you extracted real information. Use null for unknown fields."""

    messages = []
    if not hist:
        messages.append({
            "role": "assistant",
            "content": "Hi " + user['name'] + "! I'm your cycling coach. Before we dive into your rides, I'd love to learn a bit about you. Tell me — how long have you been cycling, and what got you into it?"
        })
    
    for h in hist:
        messages.append(h)
    messages.append({"role": "user", "content": message})

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 500,
                      "system": system_prompt, "messages": messages},
                timeout=30
            )
            full_reply = resp.json()['content'][0]['text']
    except Exception as e:
        return {"reply": "Sorry, I had trouble connecting. Please try again.", "profile_update": {}}

    # Extract JSON profile update from last line
    profile_update = {}
    lines = full_reply.strip().split('\n')
    reply_text = full_reply
    for line in reversed(lines):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                extracted = _json.loads(line)
                profile_update = {k: v for k, v in extracted.items() if v is not None and v != False}
                reply_text = '\n'.join(lines[:-1]).strip()
                break
            except:
                pass

    # Save any extracted profile fields
    if profile_update:
        conn = get_db(); cur = conn.cursor()
        fields = list(profile_update.keys())
        vals = [profile_update[f] for f in fields]
        set_clause = ', '.join(f + '=%s' for f in fields)
        cur.execute(
            "INSERT INTO profiles (user_id, " + ', '.join(fields) + ") VALUES (%s" + ',%s'*len(fields) + ") "
            "ON CONFLICT (user_id) DO UPDATE SET " + set_clause + ", updated_at=NOW()",
            [user['id']] + vals + vals
        )
        cur.close(); conn.close()

    return {"reply": reply_text, "profile_update": profile_update}

# ── Strava Integration ───────────────────────────────────────────────────────

@app.get("/debug/dashboard")
def debug_dashboard(user: dict = Depends(get_current_user)):
    """Debug endpoint to show dashboard error details."""
    import traceback
    try:
        conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM profiles WHERE user_id=%s", (user['id'],))
        profile = cur.fetchone()
        user_goal = int(profile['annual_goal_mi']) if profile and profile.get('annual_goal_mi') else ANNUAL_GOAL
        cur.execute("SELECT * FROM rides WHERE user_id=%s AND ride_date >= %s AND ride_date < %s ORDER BY ride_date ASC LIMIT 5",
            (user['id'], f'{YEAR}-01-01', f'{YEAR+1}-01-01'))
        rides = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
        result = build_full_dashboard(rides, user['name'], annual_goal=user_goal)
        return {"status": "ok", "html_length": len(result), "rides": len(rides), "goal": user_goal}
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

@app.get("/strava/connect")
def strava_connect(_auth: str = ""):
    """Redirect user to Strava OAuth page. Token passed as _auth query param."""
    from urllib.parse import urlencode
    from fastapi.responses import RedirectResponse, HTMLResponse
    if not _auth:
        return HTMLResponse("<h2>Missing auth token. Please try again from the app.</h2>")
    # Verify token
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE token=%s", (_auth,))
    user = cur.fetchone(); cur.close(); conn.close()
    if not user:
        return HTMLResponse("<h2>Invalid session. Please log in again.</h2>")
    params = {
        "client_id":       STRAVA_CLIENT_ID,
        "redirect_uri":    STRAVA_REDIRECT_URI,
        "response_type":   "code",
        "approval_prompt": "auto",
        "scope":           "activity:read_all",
        "state":           _auth
    }
    return RedirectResponse(STRAVA_AUTH_URL + "?" + urlencode(params))

@app.get("/strava/callback")
async def strava_callback(code: str, state: str, error: str = None):
    """Handle Strava OAuth callback — exchange code for tokens."""
    from fastapi.responses import HTMLResponse
    if error:
        return HTMLResponse("<h2>Strava connection cancelled.</h2><p>You can close this window.</p>")
    
    # Verify state is a valid user token
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE token=%s", (state,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        return HTMLResponse("<h2>Invalid session. Please try again.</h2>")

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(STRAVA_TOKEN_URL, data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code"
        })
        data = resp.json()

    if "access_token" not in data:
        cur.close(); conn.close()
        return HTMLResponse("<h2>Strava connection failed.</h2><p>" + str(data) + "</p>")

    # Store tokens
    cur.execute("""
        INSERT INTO strava_tokens (user_id, athlete_id, access_token, refresh_token, expires_at)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET
            athlete_id=EXCLUDED.athlete_id,
            access_token=EXCLUDED.access_token,
            refresh_token=EXCLUDED.refresh_token,
            expires_at=EXCLUDED.expires_at
    """, (user['id'], data.get('athlete',{}).get('id'),
          data['access_token'], data['refresh_token'], data['expires_at']))
    cur.close(); conn.close()

    return HTMLResponse("""
        <html><body style="font-family:Inter,sans-serif;text-align:center;padding:40px;">
        <h2 style="color:#27AE60;">✓ Strava Connected!</h2>
        <p>Your Strava account is now linked. You can close this window and return to the app.</p>
        <script>setTimeout(()=>window.close(),3000);</script>
        </body></html>
    """)

@app.get("/strava/status")
def strava_status(user: dict = Depends(get_current_user)):
    """Check if user has Strava connected."""
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT athlete_id, last_sync FROM strava_tokens WHERE user_id=%s", (user['id'],))
    token = cur.fetchone(); cur.close(); conn.close()
    return {"connected": token is not None, "last_sync": str(token['last_sync']) if token and token['last_sync'] else None}

@app.post("/strava/sync")
async def strava_sync(
    days_back: int = Form(default=90),
    user: dict = Depends(get_current_user)
):
    """Pull recent activities from Strava and store them."""
    import time
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM strava_tokens WHERE user_id=%s", (user['id'],))
    token_row = cur.fetchone()
    if not token_row:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Strava not connected")

    # Refresh token if expired
    access_token = token_row['access_token']
    if token_row['expires_at'] and int(time.time()) > token_row['expires_at'] - 300:
        async with httpx.AsyncClient() as client:
            resp = await client.post(STRAVA_TOKEN_URL, data={
                "client_id":     STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "grant_type":    "refresh_token",
                "refresh_token": token_row['refresh_token']
            })
            new_tokens = resp.json()
        if "access_token" in new_tokens:
            access_token = new_tokens['access_token']
            cur2 = conn.cursor()
            cur2.execute("UPDATE strava_tokens SET access_token=%s, refresh_token=%s, expires_at=%s WHERE user_id=%s",
                        (access_token, new_tokens['refresh_token'], new_tokens['expires_at'], user['id']))
            cur2.close()

    # Fetch activities — never go further back than Jan 1 of YEAR,
    # regardless of days_back. This is what caused the 2024/2025 leak.
    days_back_ts  = int(time.time()) - (days_back * 86400)
    year_start_ts = int(datetime(YEAR, 1, 1).timestamp())
    after_ts = max(days_back_ts, year_start_ts)
    imported = 0; skipped = 0; errors = 0; out_of_range = 0
    page = 1

    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                "https://www.strava.com/api/v3/athlete/activities",
                headers={"Authorization": "Bearer " + access_token},
                params={"after": after_ts, "per_page": 50, "page": page}
            )
            activities = resp.json()
            if not activities or not isinstance(activities, list):
                break

            for act in activities:
                try:
                    act_date = act.get('start_date_local','')[:10]
                    # Hard safety net: skip anything outside the current YEAR
                    # even if it slipped past the after_ts filter (e.g. local
                    # timezone landing an activity just before Jan 1).
                    if act_date < f'{YEAR}-01-01' or act_date >= f'{YEAR+1}-01-01':
                        out_of_range += 1
                        continue
                    dist_mi  = round((act.get('distance') or 0) / 1609.34, 2)
                    dur_h    = round((act.get('moving_time') or 0) / 3600, 2)
                    sport    = act.get('sport_type','').lower()
                    is_virt  = act.get('trainer', False) or 'virtual' in sport or 'zwift' in (act.get('name','') or '').lower()

                    # Deduplication
                    cur3 = conn.cursor()
                    cur3.execute("""SELECT id FROM rides WHERE user_id=%s AND ride_date=%s
                        AND ABS(COALESCE(dist_mi,0)-%s)<0.5 AND ABS(COALESCE(duration_h,0)-%s)<0.1""",
                        (user['id'], act_date, dist_mi, dur_h))
                    if cur3.fetchone():
                        cur3.close(); skipped += 1; continue

                    # Get stream data for power/HR/cadence
                    stream_resp = await client.get(
                        f"https://www.strava.com/api/v3/activities/{act['id']}/streams",
                        headers={"Authorization": "Bearer " + access_token},
                        params={"keys": "watts,heartrate,cadence,altitude", "key_by_type": "true"}
                    )
                    streams = stream_resp.json()

                    def stream_vals(key):
                        s = streams.get(key,{})
                        return s.get('data',[]) if isinstance(s, dict) else []

                    powers   = [v for v in stream_vals('watts')     if v and v > 0]
                    hrs      = [v for v in stream_vals('heartrate')  if v]
                    cads     = [v for v in stream_vals('cadence')    if v]
                    alts     = stream_vals('altitude')

                    def best_avg(vals, n):
                        if not vals or len(vals) < n: return max(vals) if vals else None
                        return round(max(sum(vals[i:i+n])/n for i in range(len(vals)-n+1)))

                    np_val = None
                    if powers and len(powers) > 30:
                        sm = [sum(powers[max(0,i-29):i+1])/len(powers[max(0,i-29):i+1]) for i in range(len(powers))]
                        np_val = round((sum(x**4 for x in sm)/len(sm))**0.25)

                    elev_ft = 0.0
                    if alts and len(alts) > 1:
                        for i in range(1, len(alts)):
                            d = alts[i] - alts[i-1]
                            if d > 0: elev_ft += d
                    elev_ft = round(elev_ft * 3.28084) if elev_ft else (round((act.get('total_elevation_gain') or 0) * 3.28084))

                    avg_power = round(sum(powers)/len(powers)) if powers else None
                    avg_hr    = round(sum(hrs)/len(hrs)) if hrs else None
                    avg_cad   = round(sum(cads)/len(cads)) if cads else None

                    ride_type = classify_ride(dist_mi, dur_h, avg_power, is_virt)

                    cur3.execute("""INSERT INTO rides (user_id,ride_date,name,dist_mi,duration_h,
                        avg_power,norm_power,avg_hr,max_hr,avg_cadence,max_cadence,
                        p5,p15,p30,elev_gain_ft,ride_type,is_virtual,temp_c,notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (user['id'], act_date, act.get('name','Activity'),
                         dist_mi, dur_h, avg_power, np_val,
                         avg_hr, act.get('max_heartrate'),
                         avg_cad, max(cads) if cads else None,
                         best_avg(powers,5), best_avg(powers,15), best_avg(powers,30),
                         elev_ft, ride_type, is_virt,
                         act.get('average_temp'), None))
                    cur3.close()
                    imported += 1
                except Exception as e:
                    errors += 1

            if len(activities) < 50:
                break
            page += 1

    # Update last sync time
    cur.execute("UPDATE strava_tokens SET last_sync=NOW() WHERE user_id=%s", (user['id'],))
    cur.close(); conn.close()

    return {"imported": imported, "skipped": skipped, "errors": errors, "out_of_range": out_of_range,
            "message": f"Synced {imported} new activities from Strava ({skipped} already existed, "
                       f"{out_of_range} outside {YEAR})"}

@app.delete("/rides/clear")
def clear_rides(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM rides WHERE user_id=%s", (user['id'],))
    cur.close(); conn.close()
    return {"status": "all rides cleared"}

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(user: dict = Depends(get_current_user)):
    try:
        conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM profiles WHERE user_id=%s", (user['id'],))
        profile = cur.fetchone()
        user_goal = int(profile['annual_goal_mi']) if profile and profile.get('annual_goal_mi') else ANNUAL_GOAL
        cur.execute("""SELECT * FROM rides WHERE user_id=%s
            AND ride_date >= %s AND ride_date < %s
            ORDER BY ride_date ASC""",
            (user['id'], f'{YEAR}-01-01', f'{YEAR+1}-01-01'))
        rides = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
        html = build_full_dashboard(rides, user['name'], annual_goal=user_goal)
        return HTMLResponse(content=html)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("DASHBOARD ERROR:", tb)
        return HTMLResponse(content="<pre style='color:red;padding:20px;'>" + tb + "</pre>", status_code=500)
