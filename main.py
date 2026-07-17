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
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
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
        sport   = str(session.get('sport', '')).lower()
        is_virtual = 'virtual' in sport or 'indoor' in sport or 'zwift' in sport.lower()

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
    js_virt  = ("barChart('virtualBar',['Miles','Hours'],"
                "[{label:'Virtual',data:[" + str(virt_mi) + "," + str(virt_hrs) + "],backgroundColor:PURPLE+'CC'},"
                "{label:'Outdoor',data:[" + str(out_mi)  + "," + str(out_hrs)  + "],backgroundColor:GREEN+'CC'}]);")
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

    pace_color = '#27AE60' if pace_ahead else '#E67E22'
    pace_word  = 'ahead of' if pace_ahead else 'behind'
    pace_bg    = '#E8F8F0' if pace_ahead else '#FEF0E8'

    return (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1.0'>"
        "<title>" + name + "'s Cycling Dashboard " + str(YEAR) + "</title>"
        "<script src='https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'></script>"
        "<style>"
        ":root{--blue:#1F4E79;--blue2:#2E75B6;--blue3:#D6E4F0;--green:#27AE60;--orange:#E67E22;--red:#E74C3C;--purple:#9B59B6;--grey:#F5F7FA;--text:#2C3E50;--card:#FFFFFF;}"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "body{font-family:'Segoe UI',Arial,sans-serif;background:var(--grey);color:var(--text);padding:20px;}"
        "h1{color:var(--blue);font-size:1.6rem;margin-bottom:4px;}"
        ".subtitle{color:#666;font-size:0.9rem;margin-bottom:20px;}"
        ".stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px;}"
        ".stat-card{background:var(--card);border-radius:10px;padding:14px 16px;box-shadow:0 2px 6px rgba(0,0,0,0.07);border-left:4px solid var(--blue2);}"
        ".stat-card .label{font-size:0.7rem;color:#888;text-transform:uppercase;letter-spacing:.05em;}"
        ".stat-card .value{font-size:1.5rem;font-weight:700;color:var(--blue);margin:4px 0 2px;}"
        ".stat-card .sub{font-size:0.75rem;color:#666;}"
        ".stat-card.green{border-left-color:var(--green);}"
        ".stat-card.orange{border-left-color:var(--orange);}"
        ".stat-card.purple{border-left-color:var(--purple);}"
        ".progress-wrap{background:var(--card);border-radius:10px;padding:14px 18px;box-shadow:0 2px 6px rgba(0,0,0,0.07);margin-bottom:20px;}"
        ".progress-label{display:flex;justify-content:space-between;font-size:0.82rem;color:#666;margin-bottom:6px;}"
        ".progress-bar-bg{background:#E0E0E0;border-radius:8px;height:16px;overflow:hidden;}"
        ".progress-bar-fill{height:100%;border-radius:8px;background:linear-gradient(90deg,var(--blue2),var(--green));}"
        ".charts-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:16px;}"
        ".chart-card{background:var(--card);border-radius:10px;padding:16px 18px;box-shadow:0 2px 6px rgba(0,0,0,0.07);}"
        ".chart-card h3{font-size:0.88rem;color:var(--blue);margin-bottom:12px;font-weight:600;}"
        ".chart-card canvas{max-height:260px;}"
        ".section-header{margin:24px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--blue3);}"
        ".section-header h2{color:var(--blue);font-size:1rem;font-weight:700;}"
        ".section-header p{font-size:0.75rem;color:#888;margin-top:3px;}"
        ".footer{text-align:center;color:#aaa;font-size:0.75rem;margin-top:20px;padding-top:12px;border-top:1px solid #eee;}"
        "@media(max-width:600px){.charts-grid{grid-template-columns:1fr;}.stats-grid{grid-template-columns:repeat(2,1fr);}}"
        "</style></head><body>"

        "<h1>&#x1F6B4; " + name + "'s Cycling Dashboard " + str(YEAR) + "</h1>"
        "<p class='subtitle'>Updated " + date.today().strftime('%B %d, %Y') + " &nbsp;&middot;&nbsp; "
        + str(len(rides)) + " rides &nbsp;&middot;&nbsp; Goal: " + str(goal) + " miles</p>"

        "<div class='stats-grid'>"
        "<div class='stat-card green'><div class='label'>Year Total</div>"
        "<div class='value'>" + str(round(total_mi,1)) + "</div>"
        "<div class='sub'>miles &nbsp;(" + str(pct_complete) + "% of goal)</div></div>"

        "<div class='stat-card'><div class='label'>Remaining</div>"
        "<div class='value'>" + str(round(remaining,1)) + "</div>"
        "<div class='sub'>miles to " + str(goal) + "</div></div>"

        "<div class='stat-card " + ("green" if pace_ahead else "orange") + "'>"
        "<div class='label'>Pace</div>"
        "<div class='value'>" + str(abs(pace_diff)) + "</div>"
        "<div class='sub'>miles " + pace_word + " pace</div></div>"

        "<div class='stat-card'><div class='label'>Hours in Saddle</div>"
        "<div class='value'>" + str(round(total_hrs,1)) + "</div>"
        "<div class='sub'>hours total</div></div>"

        "<div class='stat-card'><div class='label'>Total Rides</div>"
        "<div class='value'>" + str(len(rides)) + "</div>"
        "<div class='sub'>" + str(virt_count) + " virtual &nbsp;&middot;&nbsp; " + str(out_count) + " outdoor</div></div>"

        "<div class='stat-card purple'><div class='label'>Elevation</div>"
        "<div class='value'>" + str(int(total_elev)) + "</div>"
        "<div class='sub'>feet climbed total</div></div>"
        "</div>"

        "<div class='progress-wrap'>"
        "<div class='progress-label'>"
        "<span><strong>" + str(round(total_mi,1)) + " mi</strong> completed</span>"
        "<span>Goal: <strong>" + str(goal) + " mi</strong></span>"
        "</div>"
        "<div class='progress-bar-bg'>"
        "<div class='progress-bar-fill' style='width:" + str(min(pct_complete,100)) + "%'></div>"
        "</div></div>"

        "<div class='charts-grid'>"
        "<div class='chart-card'><h3>&#x1F4C5; Weekly Mileage vs " + str(WEEKLY_TARGET) + "-Mile Target</h3><canvas id='weeklyBar'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F4C8; Cumulative Miles vs Annual Target</h3><canvas id='cumulativeLine'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F4C6; Monthly Miles</h3><canvas id='monthlyBar'></canvas></div>"
        "<div class='chart-card'><h3>&#x23F1; Hours in the Saddle by Month</h3><canvas id='monthlyHours'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F3F7; Ride Type &#x2014; Miles</h3><canvas id='rtypeMiles'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F3F7; Ride Type &#x2014; Hours</h3><canvas id='rtypeHours'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F7E3; Virtual vs Outdoor</h3><canvas id='virtualBar'></canvas></div>"
        "<div class='chart-card'><h3>&#x26F0; Elevation Gain per Ride (ft)</h3><canvas id='elevBar'></canvas></div>"
        "<div class='chart-card'><h3>&#x26A1; Average Power per Ride (W)</h3><canvas id='powerLine'></canvas></div>"
        "<div class='chart-card'><h3>&#x2764; Average Heart Rate per Ride (bpm)</h3><canvas id='hrLine'></canvas></div>"
        "</div>"

        "<div class='section-header'>"
        "<h2>&#x1F3C6; Coaching Analytics &#x2014; Power &middot; Heart Rate &middot; Cadence &middot; Sprint Power</h2>"
        "<p>Solid line = average &nbsp;&middot;&nbsp; Dashed line = max/normalized &nbsp;&middot;&nbsp; All rides &#x2265; 5 miles</p>"
        "</div>"

        "<div class='charts-grid'>"
        "<div class='chart-card'><h3>&#x26A1; Avg Power vs Normalized Power (W)</h3><canvas id='coachPower'></canvas></div>"
        "<div class='chart-card'><h3>&#x2764;&#xFE0F; Avg HR vs Max HR (bpm)</h3><canvas id='coachHR'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F504; Avg Cadence vs Max Cadence (rpm)</h3><canvas id='coachCad'></canvas></div>"
        "<div class='chart-card'><h3>&#x1F3CE;&#xFE0F; Sprint Power &#x2014; 5s / 15s / 30s Best (W)</h3><canvas id='coachSprint'></canvas></div>"
        "</div>"

        "<p class='footer'>Generated by Cycling Coach &nbsp;&middot;&nbsp; " + date.today().strftime('%Y-%m-%d') + "</p>"

        "<script>"
        "const BLUE='#2E75B6',GREEN='#27AE60',ORANGE='#E67E22',RED='#E74C3C',PURPLE='#9B59B6',GREY='#95A5A6';"
        "const TYPE_COLORS=[GREY,ORANGE,BLUE,GREEN,'#F39C12',RED,GREEN];"
        "Chart.defaults.font.family=\"'Segoe UI',Arial,sans-serif\";"
        "Chart.defaults.font.size=11;Chart.defaults.color='#555';"
        "function barChart(id,labels,datasets,opts){"
        "opts=opts||{};"
        "new Chart(document.getElementById(id),{type:'bar',data:{labels:labels,datasets:datasets},"
        "options:Object.assign({responsive:true,plugins:{legend:{display:datasets.length>1}},"
        "scales:{y:{beginAtZero:true},x:{ticks:{maxRotation:45}}}},opts)});}"
        "function lineChart(id,labels,datasets,opts){"
        "opts=opts||{};"
        "new Chart(document.getElementById(id),{type:'line',data:{labels:labels,datasets:datasets},"
        "options:Object.assign({responsive:true,plugins:{legend:{display:datasets.length>1}},"
        "scales:{y:{beginAtZero:false},x:{ticks:{maxRotation:45}}},"
        "elements:{point:{radius:2},line:{tension:0.3}}},opts)});}"
        + js_weekly
        + js_cumul
        + js_mo_mi
        + js_mo_hr
        + js_rt_mi
        + js_rt_hr
        + js_virt
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
        cur.close(); conn.close()
        prompt = (
            "Rider: " + user['name'] + "\n"
            + ("Personal notes: " + "; ".join(notes) + "\n" if notes else "")
            + "Latest ride: " + str(metrics.get('ride_date',''))
            + ", " + str(metrics.get('dist_mi','')) + " mi"
            + ", avg power " + str(metrics.get('avg_power','')) + "W"
            + ", NP " + str(metrics.get('norm_power','')) + "W"
            + ", avg HR " + str(metrics.get('avg_hr','')) + " bpm"
            + ", max HR " + str(metrics.get('max_hr','')) + " bpm"
            + ", cadence " + str(metrics.get('avg_cadence','')) + " rpm"
            + ", elevation " + str(metrics.get('elev_gain_ft','')) + " ft"
            + ", temp " + str(metrics.get('temp_c','')) + "C\n"
            "Give a 3-4 sentence coaching assessment. Be direct and specific."
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

@app.delete("/rides/clear")
def clear_rides(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM rides WHERE user_id=%s", (user['id'],))
    cur.close(); conn.close()
    return {"status": "all rides cleared"}

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM rides WHERE user_id=%s ORDER BY ride_date ASC", (user['id'],))
    rides = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return HTMLResponse(content=build_full_dashboard(rides, user['name']))
