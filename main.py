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
from datetime import datetime, date
import httpx

app = FastAPI(title="Cycling Coach API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATABASE_URL  = os.environ.get("DATABASE_URL", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
security      = HTTPBearer()

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def init_db():
    conn = get_db()
    cur  = conn.cursor()
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
            avg_cadence INTEGER, max_cadence INTEGER, p5 INTEGER, p15 INTEGER,
            p30 INTEGER, temp_c FLOAT, notes TEXT, created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS coaching_notes (
            id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id),
            note TEXT, created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.close(); conn.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE token = %s", (credentials.credentials,))
    user = cur.fetchone()
    cur.close(); conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user

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
        hrs      = [r['heart_rate'] for r in records if r.get('heart_rate')]
        cadences = [r['cadence']    for r in records if r.get('cadence')]
        def best_avg(vals, n):
            if not vals or len(vals) < n: return max(vals) if vals else None
            return round(max(sum(vals[i:i+n])/n for i in range(len(vals)-n+1)))
        np_val = None
        if powers and len(powers) > 30:
            smoothed = [sum(powers[max(0,i-29):i+1])/len(powers[max(0,i-29):i+1]) for i in range(len(powers))]
            np_val = round((sum(x**4 for x in smoothed)/len(smoothed))**0.25)
        start = session.get('start_time')
        dist  = session.get('total_distance')
        elapsed = session.get('total_elapsed_time')
        return {
            'ride_date':   start.strftime('%Y-%m-%d') if hasattr(start,'strftime') else str(start)[:10],
            'name':        session.get('sport','Ride'),
            'dist_mi':     round(dist/1609.34,2) if dist else None,
            'duration_h':  round(elapsed/3600,2) if elapsed else None,
            'avg_power':   session.get('avg_power'),
            'norm_power':  session.get('normalized_power') or np_val,
            'avg_hr':      session.get('avg_heart_rate'),
            'max_hr':      session.get('max_heart_rate'),
            'avg_cadence': session.get('avg_cadence'),
            'max_cadence': max(cadences) if cadences else None,
            'p5':  best_avg(powers, 5),
            'p15': best_avg(powers, 15),
            'p30': best_avg(powers, 30),
            'temp_c': session.get('avg_temperature'),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail="Could not parse FIT file: " + str(e))

def build_dashboard_html(rides, name):
    def fmt(r):
        d = r['ride_date']
        return d.strftime('%b %d') if hasattr(d,'strftime') else str(d)[:10]
    dates   = [fmt(r) for r in rides]
    avgpwr  = [r['avg_power']   for r in rides]
    np_vals = [r['norm_power']  for r in rides]
    avghr   = [r['avg_hr']      for r in rides]
    maxhr   = [r['max_hr']      for r in rides]
    avgcad  = [r['avg_cadence'] for r in rides]
    maxcad  = [r['max_cadence'] for r in rides]
    p5      = [r['p5']  for r in rides]
    p15     = [r['p15'] for r in rides]
    p30     = [r['p30'] for r in rides]
    p15_mid = [(b or 0)-(a or 0) for a,b in zip(p30,p15)]
    p5_top  = [(b or 0)-(a or 0) for a,b in zip(p15,p5)]

    jd   = json.dumps(dates)
    jaw  = json.dumps(avgpwr)
    jnp  = json.dumps(np_vals)
    jah  = json.dumps(avghr)
    jmh  = json.dumps(maxhr)
    jac  = json.dumps(avgcad)
    jmc  = json.dumps(maxcad)
    jp5  = json.dumps(p5)
    jp15 = json.dumps(p15)
    jp30 = json.dumps(p30)
    jpm  = json.dumps(p15_mid)
    jpt  = json.dumps(p5_top)
    td   = date.today().strftime('%B %d, %Y')
    nr   = str(len(rides))

    # Build JS without f-strings to avoid brace conflicts
    js = (
        "const L=" + jd + ";"
        "function mkL(id,d1,d2,c){"
        "new Chart(document.getElementById(id),{type:'line',data:{labels:L,datasets:["
        "{label:d1.l,data:d1.d,borderColor:c,backgroundColor:c+'22',borderWidth:2,pointRadius:3,tension:0.3,spanGaps:true},"
        "{label:d2.l,data:d2.d,borderColor:c,borderDash:[5,4],borderWidth:2,pointRadius:2,tension:0.3,spanGaps:true}"
        "]},options:{responsive:true,plugins:{legend:{display:false},tooltip:{mode:'index',intersect:false}},"
        "scales:{x:{ticks:{font:{size:9}},grid:{display:false}},y:{ticks:{font:{size:9}},grid:{color:'#eee'}}}}});}"
        "mkL('pw',{l:'Avg',d:" + jaw + "},{l:'NP',d:" + jnp + "},'#3b82f6');"
        "mkL('hr',{l:'Avg HR',d:" + jah + "},{l:'Max HR',d:" + jmh + "},'#ef4444');"
        "mkL('cad',{l:'Avg Cad',d:" + jac + "},{l:'Max Cad',d:" + jmc + "},'#f59e0b');"
        "var p30=" + jp30 + ",p15=" + jp15 + ",p5=" + jp5 + ";"
        "new Chart(document.getElementById('sp'),{type:'bar',data:{labels:L,datasets:["
        "{label:'30s',data:" + jp30 + ",backgroundColor:'#22c55e',stack:'s'},"
        "{label:'15s',data:" + jpm  + ",backgroundColor:'#3b82f6',stack:'s'},"
        "{label:'5s', data:" + jpt  + ",backgroundColor:'#f59e0b',stack:'s'}"
        "]},options:{responsive:true,"
        "plugins:{legend:{display:false},tooltip:{mode:'index',intersect:false,"
        "itemSort:function(a,b){return b.datasetIndex-a.datasetIndex;},"
        "callbacks:{label:function(ctx){var i=ctx.dataIndex;"
        "if(ctx.datasetIndex===2)return '5s: '+(p5[i]||'-')+'W';"
        "if(ctx.datasetIndex===1)return '15s: '+(p15[i]||'-')+'W';"
        "return '30s: '+(p30[i]||'-')+'W';}}}},"
        "scales:{x:{stacked:true,ticks:{font:{size:9}},grid:{display:false}},"
        "y:{stacked:true,ticks:{font:{size:9}},grid:{color:'#eee'}}}}});"
    )

    css = (
        "body{background:#f0f0f0;font-family:Inter,sans-serif;padding:20px;margin:0;}"
        "h1{font-size:20px;font-weight:600;margin-bottom:4px;}"
        ".sub{font-size:12px;color:#888;margin-bottom:20px;}"
        ".grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}"
        ".card{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px;}"
        ".card h3{font-size:12px;font-weight:600;color:#333;margin-bottom:8px;}"
        "canvas{width:100%!important;height:200px!important;}"
    )

    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<title>" + name + "'s Cycling Dashboard</title>"
        "<script src='https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js'></script>"
        "<style>" + css + "</style></head><body>"
        "<h1>&#x1F6B4; " + name + "'s Cycling Dashboard</h1>"
        "<div class='sub'>Updated " + td + " &middot; " + nr + " rides</div>"
        "<div class='grid'>"
        "<div class='card'><h3>&#x26A1; Avg Power vs NP (W)</h3><canvas id='pw'></canvas></div>"
        "<div class='card'><h3>&#x2764; Avg HR vs Max HR (bpm)</h3><canvas id='hr'></canvas></div>"
        "<div class='card'><h3>&#x1F504; Avg vs Max Cadence (rpm)</h3><canvas id='cad'></canvas></div>"
        "<div class='card'><h3>&#x1F3CE; Sprint Power 5s/15s/30s (W)</h3><canvas id='sp'></canvas></div>"
        "</div>"
        "<script>" + js + "</script>"
        "</body></html>"
    )

async def get_coaching_summary(user, metrics):
    if not ANTHROPIC_KEY:
        return "AI coaching unavailable."
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM rides WHERE user_id=%s ORDER BY ride_date DESC LIMIT 10", (user['id'],))
        recent = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT note FROM coaching_notes WHERE user_id=%s ORDER BY created_at DESC LIMIT 5", (user['id'],))
        notes = [r['note'] for r in cur.fetchall()]
        cur.close(); conn.close()
        prompt = (
            "Rider: " + user['name'] + "\n" +
            ("Notes: " + "; ".join(notes) + "\n" if notes else "") +
            "Latest ride: " + str(metrics['ride_date']) +
            ", " + str(metrics['dist_mi']) + " mi" +
            ", avg power " + str(metrics['avg_power']) + "W" +
            ", NP " + str(metrics['norm_power']) + "W" +
            ", avg HR " + str(metrics['avg_hr']) + " bpm" +
            ", max HR " + str(metrics['max_hr']) + " bpm" +
            ", cadence " + str(metrics['avg_cadence']) + " rpm" +
            ", temp " + str(metrics['temp_c']) + "C\n"
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
        uid = cur.fetchone()[0]
        cur.close(); conn.close()
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
    cur.execute("""INSERT INTO rides (user_id,ride_date,name,dist_mi,duration_h,
        avg_power,norm_power,avg_hr,max_hr,avg_cadence,max_cadence,p5,p15,p30,temp_c,notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (user['id'],metrics['ride_date'],metrics['name'],metrics['dist_mi'],metrics['duration_h'],
         metrics['avg_power'],metrics['norm_power'],metrics['avg_hr'],metrics['max_hr'],
         metrics['avg_cadence'],metrics['max_cadence'],metrics['p5'],metrics['p15'],
         metrics['p30'],metrics['temp_c'],notes))
    ride_id = cur.fetchone()[0]; cur.close(); conn.close()
    coaching = await get_coaching_summary(user, metrics)
    return {"ride_id": ride_id, "metrics": metrics, "coaching": coaching}

@app.get("/rides")
def get_rides(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM rides WHERE user_id=%s ORDER BY ride_date DESC LIMIT 100", (user['id'],))
    rides = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return {"rides": rides}

@app.post("/notes")
def add_note(note: str = Form(...), user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO coaching_notes (user_id,note) VALUES (%s,%s)", (user['id'],note))
    cur.close(); conn.close()
    return {"status": "saved"}

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(user: dict = Depends(get_current_user)):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM rides WHERE user_id=%s ORDER BY ride_date ASC", (user['id'],))
    rides = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return HTMLResponse(content=build_dashboard_html(rides, user['name']))
