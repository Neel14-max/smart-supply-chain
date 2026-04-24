"""
Smart Supply Chain Optimization System — Backend v8

Changes from v7:
  - NEW /api/gemini-traffic endpoint: calls Gemini 2.0 Flash to generate
    realistic real-time traffic congestion + road incident data for a corridor.
  - Gemini traffic response is structured JSON with segment-level congestion
    and incident list — returned to the frontend for display.
  - All previous fixes retained (fresh Overpass connections, POI cache, etc.)
"""

from flask import Flask, request, jsonify, send_from_directory
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, os, math, random, time, hashlib, json, re
from datetime import datetime

app = Flask(__name__, static_folder='static')

ORS_KEY    = os.environ.get("ORS_KEY", "YOUR_ORS_KEY_HERE")
OWM_KEY    = os.environ.get("OWM_KEY", "YOUR_OWM_KEY_HERE")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "YOUR_GEMINI_KEY_HERE")

NOMINATIM_URL     = "https://nominatim.openstreetmap.org/search"
ORS_DIRECTION_URL = "https://api.openrouteservice.org/v2/directions/{profile}"
OWM_URL           = "https://api.openweathermap.org/data/2.5/weather"
OVERPASS_URL      = "https://overpass-api.de/api/interpreter"
GEMINI_URL        = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

_sess = requests.Session()
_sess.headers.update({"User-Agent": "LogisticsRoute/8.0"})

_poi_cache: dict = {}
POI_CACHE_TTL = 600

ROUTE_COLORS = [
    "#2563eb","#dc2626","#16a34a","#d97706",
    "#7c3aed","#0891b2","#059669","#db2777",
    "#ea580c","#4338ca",
]

ORS_STRATEGIES = [
    ("recommended", True,  1.2, 0.80),
    ("recommended", True,  1.4, 0.65),
    ("fastest",     True,  1.2, 0.80),
    ("fastest",     True,  1.4, 0.65),
    ("shortest",    True,  1.2, 0.80),
    ("shortest",    True,  1.4, 0.65),
    ("recommended", False, None, None),
    ("fastest",     False, None, None),
    ("shortest",    False, None, None),
]

WAYPOINT_OFFSETS = [
    (+0.55, 0.00), (-0.55, 0.00),
    ( 0.00,+0.65), ( 0.00,-0.65),
    (+0.40,+0.40), (-0.40,-0.40),
]

ACCIDENT_ZONES = [
    {"name":"NH-48 Vadodara bypass",    "lat":22.30,"lng":73.19,"radius_km":30},
    {"name":"NH-8 Vapi-Surat stretch",  "lat":20.38,"lng":72.90,"radius_km":25},
    {"name":"Mumbai-Pune Expressway",   "lat":18.74,"lng":73.40,"radius_km":40},
    {"name":"NH-44 Nagpur section",     "lat":21.14,"lng":79.09,"radius_km":35},
    {"name":"Delhi-Gurgaon NH-48",      "lat":28.46,"lng":77.03,"radius_km":20},
    {"name":"NH-19 Kanpur section",     "lat":26.44,"lng":80.33,"radius_km":30},
    {"name":"Bengaluru ring road",      "lat":12.97,"lng":77.59,"radius_km":25},
    {"name":"Chennai-Tambaram NH-32",   "lat":12.92,"lng":80.12,"radius_km":20},
    {"name":"Ahmedabad-Gandhinagar NH", "lat":23.03,"lng":72.58,"radius_km":18},
    {"name":"Hyderabad ORR stretch",    "lat":17.38,"lng":78.48,"radius_km":28},
]

FACTOR_WEIGHTS = {
    "weather":2.5,"accident":2.5,"traffic":2.0,
    "road":1.5,   "distance":1.0,"duration":0.5,
}

MIN_ROUTES = 4
MAX_ROUTES = 7


# ── Utilities ──────────────────────────────────────────────────────────────────

def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    dlat = math.radians(lat2-lat1); dlng = math.radians(lng2-lng1)
    a = (math.sin(dlat/2)**2
         + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlng/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def decode_polyline(enc, precision=5):
    coords=[]; idx=lat=lng=0; f=10**precision
    while idx < len(enc):
        for is_lng in (False, True):
            shift=res=0
            while True:
                b=ord(enc[idx])-63; idx+=1; res|=(b&0x1F)<<shift; shift+=5
                if b<0x20: break
            d=~(res>>1) if res&1 else res>>1
            if is_lng: lng+=d
            else:      lat+=d
        coords.append([lat/f, lng/f])
    return coords

def route_fp(geo, buckets=16):
    if not geo: return ()
    step = max(1, len(geo)//buckets)
    return tuple((round(p[0],1), round(p[1],1)) for p in geo[::step][:buckets])

def similarity(ga, gb):
    a=set(route_fp(ga)); b=set(route_fp(gb))
    return len(a&b)/max(len(a),len(b)) if a and b else 0.0

def is_dup(ng, existing, thresh=0.55):
    return any(similarity(ng, eg) > thresh for eg in existing)

def geo_from_raw(raw):
    rg = raw.get("geometry","")
    return (decode_polyline(rg) if isinstance(rg,str)
            else [[c[1],c[0]] for c in rg.get("coordinates",[])])

def route_bbox(geo, pad=0.3):
    lats=[p[0] for p in geo]; lngs=[p[1] for p in geo]
    return (round(min(lats)-pad,4), round(min(lngs)-pad,4),
            round(max(lats)+pad,4), round(max(lngs)+pad,4))

def sample_geo(geo, n=6):
    if not geo: return []
    if len(geo) <= n: return geo
    step = len(geo)//n
    return [geo[i*step] for i in range(n)]


# ── Geocoding ──────────────────────────────────────────────────────────────────

def geocode(place):
    try:
        r = _sess.get(NOMINATIM_URL,
                      params={"q":place,"format":"json","limit":1},
                      timeout=8).json()
        if r:
            lat, lng = float(r[0]["lat"]), float(r[0]["lon"])
            return {"lat":lat,"lng":lng,"label":r[0].get("display_name",place)}
    except Exception as e:
        print(f"[Geocode ERROR] {e}")
    return None


# ── Weather ────────────────────────────────────────────────────────────────────

def get_weather(lat, lng, label=""):
    try:
        d = _sess.get(OWM_URL,
                      params={"lat":lat,"lon":lng,"appid":OWM_KEY,"units":"metric"},
                      timeout=8).json()
        if "weather" not in d: raise ValueError("bad OWM response")
        wx = {
            "condition":     d["weather"][0]["main"],
            "description":   d["weather"][0]["description"],
            "temp_c":        round(d["main"]["temp"],1),
            "humidity":      d["main"]["humidity"],
            "wind_kmh":      round(d["wind"]["speed"]*3.6,1),
            "visibility_km": round(d.get("visibility",10000)/1000,1),
            "icon":          d["weather"][0]["icon"],
            "label":         label,
        }
        risk=0; c=wx["condition"].lower()
        if "thunderstorm" in c: risk+=4
        elif "rain" in c:       risk+=2
        elif "drizzle" in c:    risk+=1
        elif "snow" in c:       risk+=3
        elif "fog" in c or "mist" in c: risk+=2
        elif "haze" in c:       risk+=1
        if wx["wind_kmh"]>60:        risk+=2
        elif wx["wind_kmh"]>40:      risk+=1
        if wx["visibility_km"]<1:    risk+=3
        elif wx["visibility_km"]<3:  risk+=2
        wx["risk_score"] = min(risk, 5)
        return wx
    except Exception as e:
        print(f"[Weather ERROR] {e}")
        return {"condition":"Unknown","description":"N/A","temp_c":0,"humidity":0,
                "wind_kmh":0,"visibility_km":10,"icon":"01d","label":label,"risk_score":0}

def fetch_weather_all(sc, ec, mid_lat, mid_lng):
    with ThreadPoolExecutor(max_workers=3) as ex:
        fo = ex.submit(get_weather, sc["lat"], sc["lng"], sc.get("label","Origin")[:30])
        fd = ex.submit(get_weather, ec["lat"], ec["lng"], ec.get("label","Dest")[:30])
        fm = ex.submit(get_weather, mid_lat, mid_lng, "midpoint")
    return fo.result(), fd.result(), fm.result()


# ── ORS helpers ────────────────────────────────────────────────────────────────

def _ors_post(body):
    hdrs = {"Authorization":ORS_KEY,"Content-Type":"application/json"}
    try:
        r = _sess.post(ORS_DIRECTION_URL.format(profile="driving-car"),
                       json=body, headers=hdrs, timeout=22)
        if r.status_code == 200:
            return r.json().get("routes",[])
        try:
            err  = r.json().get("error",{})
            code = err.get("code",0)
            msg  = err.get("message","")[:120]
        except Exception:
            code, msg = 0, r.text[:120]
        print(f"[ORS] HTTP {r.status_code} code={code}: {msg}")
        if code == 2004 and "alternative_routes" in body:
            body2 = {k:v for k,v in body.items() if k != "alternative_routes"}
            r2 = _sess.post(ORS_DIRECTION_URL.format(profile="driving-car"),
                            json=body2, headers=hdrs, timeout=22)
            if r2.status_code == 200:
                return r2.json().get("routes",[])
    except Exception as e:
        print(f"[ORS ERROR] {e}")
    return []

def fetch_direct(start, end, pref, use_alts, wf, sf):
    body = {"coordinates":[[start["lng"],start["lat"]],[end["lng"],end["lat"]]],
            "preference":pref,"instructions":True,"units":"km"}
    if use_alts and wf:
        body["alternative_routes"] = {"target_count":3,"weight_factor":wf,"share_factor":sf}
    return _ors_post(body)

def fetch_waypoint(start, end, wp):
    body = {"coordinates":[[start["lng"],start["lat"]],
                            [wp["lng"],wp["lat"]],
                            [end["lng"],end["lat"]]],
            "preference":"recommended","instructions":True,"units":"km"}
    out = []
    for rt in _ors_post(body):
        segs = rt.get("segments",[])
        out.append({"geometry":rt.get("geometry",""),
                    "summary":{"distance":sum(s.get("distance",0) for s in segs),
                               "duration":sum(s.get("duration",0) for s in segs)},
                    "segments":[{"steps":[st for s in segs for st in s.get("steps",[])]}]})
    return out


# ── Route collection ───────────────────────────────────────────────────────────

def collect_routes(start, end):
    mid_lat = (start["lat"]+end["lat"])/2
    mid_lng = (start["lng"]+end["lng"])/2
    sk      = haversine(start["lat"],start["lng"],end["lat"],end["lng"])
    scale   = max(0.25, min(1.0, sk/400.0))
    geos=[]; raws=[]

    def try_add(raw, thresh=0.55):
        if len(raws) >= MAX_ROUTES: return
        gp = geo_from_raw(raw)
        if not gp or is_dup(gp, geos, thresh): return
        geos.append(gp); raws.append(raw)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_direct,start,end,p,ua,wf,sf):(p,wf)
                for p,ua,wf,sf in ORS_STRATEGIES}
        for f in as_completed(futs):
            if len(raws) >= MAX_ROUTES: break
            for raw in (f.result() or []): try_add(raw)

    if len(raws) < MIN_ROUTES:
        wps = [{"lat":mid_lat+dlat*scale,"lng":mid_lng+dlng*scale}
               for dlat,dlng in WAYPOINT_OFFSETS]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(fetch_waypoint,start,end,wp):wp for wp in wps}
            for f in as_completed(futs):
                if len(raws) >= MAX_ROUTES: break
                for raw in (f.result() or []): try_add(raw)

    if len(raws) < MIN_ROUTES:
        for p,ua,wf,sf in ORS_STRATEGIES[:6]:
            if len(raws) >= MIN_ROUTES: break
            for raw in fetch_direct(start,end,p,ua,wf,sf): try_add(raw, thresh=0.72)

    if len(raws) < MIN_ROUTES:
        for p,ua,wf,sf in ORS_STRATEGIES:
            if len(raws) >= MIN_ROUTES: break
            for raw in fetch_direct(start,end,p,ua,wf,sf): try_add(raw, thresh=0.92)

    return raws


# ── Traffic model (heuristic fallback) ────────────────────────────────────────

def time_cong():
    h = (datetime.utcnow().hour+5) % 24
    p = {0:0.05,1:0.04,2:0.04,3:0.05,4:0.08,5:0.13,6:0.28,7:0.52,8:0.82,
         9:0.88,10:0.72,11:0.58,12:0.62,13:0.60,14:0.54,15:0.57,16:0.70,
         17:0.84,18:0.92,19:0.87,20:0.74,21:0.57,22:0.38,23:0.18}
    return p.get(h, 0.5)

def dist_road_factor(dist_km):
    if dist_km > 500:  return 0.12
    if dist_km > 200:  return 0.22
    if dist_km > 80:   return 0.38
    if dist_km > 25:   return 0.52
    return 0.68

def compute_traffic(geo, dist_km, dur_min, idx):
    tc     = time_cong()
    rc_val = dist_road_factor(dist_km)
    urban  = 0.18 if dist_km < 25 else (0.08 if dist_km < 80 else 0.0)
    rng    = random.Random(idx*1337
                           + sum(int(p[0]*10)+int(p[1]*10) for p in geo[:4] if geo)
                           + int(time.time()//900))
    cong   = min(1.0, max(0.0, tc*0.52 + rc_val*0.30 + urban + rng.uniform(-0.07,0.07)))
    lv     = ("Free Flow" if cong<0.14 else "Light"    if cong<0.33 else
              "Moderate"  if cong<0.56 else "Heavy"    if cong<0.76 else "Standstill")
    fs     = dist_km / max(dur_min/60, 0.01)
    n      = len(geo) if geo else 1
    segs   = []
    for pi,lb,ofs in zip([0,n//4,n//2,3*n//4,n-1],
                          ["Origin","25%","Mid","75%","Dest"],
                          [0.0,0.12,-0.06,0.09,-0.08]):
        sc  = min(1.0, max(0.0, cong+ofs+rng.uniform(-0.04,0.04)))
        sl  = ("Free Flow" if sc<0.14 else "Light"    if sc<0.33 else
               "Moderate"  if sc<0.56 else "Heavy"    if sc<0.76 else "Standstill")
        spd = round(max(6.0, fs*(1-sc*0.62)), 1)
        ff  = round(max(spd, fs), 1)
        segs.append({"label":lb,"level":sl,
                     "current_speed_kmh":spd,
                     "free_flow_speed_kmh":ff,
                     "flow_ratio":round(spd/max(ff,1),2),
                     "congestion_index":round(sc,2)})
    avg_spd = round(max(6.0, fs*(1-cong*0.62)), 1)
    ff_spd  = round(max(avg_spd, fs), 1)
    return {
        "level":               lv,
        "avg_speed_kmh":       avg_spd,
        "free_flow_speed_kmh": ff_spd,
        "flow_ratio":          round(avg_spd/max(ff_spd,1), 2),
        "delay_min":           round(cong*dur_min*0.38, 1),
        "congestion_index":    round(cong, 2),
        "segments":            segs,
        "source":              "OSM + IST Time-of-Day Model",
        "updated_at":          datetime.utcnow().strftime("%H:%M UTC"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Gemini Real-Time Traffic + Incidents
# ══════════════════════════════════════════════════════════════════════════════

def _build_gemini_traffic_prompt(origin: str, destination: str,
                                  origin_lat: float, origin_lng: float,
                                  dest_lat: float, dest_lng: float,
                                  dist_km: float) -> str:
    h_ist = (datetime.utcnow().hour + 5) % 24
    day_name = datetime.utcnow().strftime("%A")
    period = ("Morning Rush" if 7 <= h_ist <= 10 else
              "Evening Rush" if 16 <= h_ist <= 20 else
              "Off-Peak")
    return f"""You are a real-time Indian road traffic intelligence system.

CORRIDOR
Origin     : {origin} ({origin_lat:.4f}, {origin_lng:.4f})
Destination: {destination} ({dest_lat:.4f}, {dest_lng:.4f})
Distance   : {dist_km:.0f} km
Current IST: {h_ist:02d}:xx ({day_name}, {period})

TASK
Generate a realistic real-time traffic and incident assessment for this Indian road corridor RIGHT NOW.
Consider Indian traffic patterns: morning/evening rush hours in metros, highway vs urban congestion, 
monsoon-season factors, weekend vs weekday, typical NH (National Highway) conditions.

Return ONLY a JSON object with EXACTLY this structure (no markdown, no backticks):
{{
  "overall_level": "<Free Flow|Light|Moderate|Heavy|Standstill>",
  "avg_speed_kmh": <integer 6-120>,
  "congestion_index": <float 0.0-1.0>,
  "delay_min": <integer>,
  "period": "{period}",
  "updated_at": "<HH:MM IST>",
  "source": "Gemini 2.0 Flash Real-Time Model",
  "segments": [
    {{"label":"Origin Zone","level":"<level>","current_speed_kmh":<int>,"description":"<10 words max>"}},
    {{"label":"Mid Corridor","level":"<level>","current_speed_kmh":<int>,"description":"<10 words max>"}},
    {{"label":"Destination Zone","level":"<level>","current_speed_kmh":<int>,"description":"<10 words max>"}}
  ],
  "incidents": [
    {{
      "type": "<Road Works|Accident|Congestion|Closure|Checkpoint|Diversion>",
      "severity": "<Low|Moderate|High|Critical>",
      "delay_min": <integer 5-60>,
      "description": "<specific description, max 20 words, mention specific highway or area name>",
      "road": "<NH-48|SH-xx|city road name or empty>",
      "lat": <float — must be between origin and destination on the corridor>,
      "lng": <float — must be between origin and destination on the corridor>
    }}
  ],
  "advisory": "<One sentence traffic advisory for a freight driver — specific, actionable>",
  "confidence": "<High|Medium|Low>"
}}

RULES:
- incidents array: 0–4 items. Only include incidents if corridor is >= 80km OR traffic is Heavy/Standstill.
- lat/lng for incidents MUST be geographically between the origin and destination coordinates.
- Use realistic Indian road names, highway numbers (NH-48, NH-44, SH-17 etc.), city names.
- avg_speed_kmh must be consistent with overall_level:
  Free Flow >= 85, Light 60-84, Moderate 35-59, Heavy 15-34, Standstill < 15
- congestion_index must match: Free Flow < 0.14, Light < 0.33, Moderate < 0.56, Heavy < 0.76, Standstill >= 0.76
- Return ONLY the JSON object. No explanation outside the JSON.
"""


def call_gemini_traffic(origin: str, destination: str,
                         origin_lat: float, origin_lng: float,
                         dest_lat: float, dest_lng: float,
                         dist_km: float) -> dict | None:
    if not GEMINI_KEY:
        return None

    prompt = _build_gemini_traffic_prompt(origin, destination,
                                           origin_lat, origin_lng,
                                           dest_lat, dest_lng, dist_km)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":     0.4,
            "maxOutputTokens": 900,
            "topP":            0.9,
        },
    }
    try:
        t0   = time.time()
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=18,
        )
        elapsed = round(time.time() - t0, 2)
        print(f"[Gemini Traffic] HTTP {resp.status_code} in {elapsed}s")

        if resp.status_code != 200:
            print(f"[Gemini Traffic ERROR] {resp.text[:200]}")
            return None

        raw_text = (
            resp.json()
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        clean = re.sub(r"```(?:json)?|```", "", raw_text).strip()
        parsed = json.loads(clean)
        print(f"[Gemini Traffic] OK — level={parsed.get('overall_level','?')} "
              f"incidents={len(parsed.get('incidents',[]))}")
        return parsed

    except json.JSONDecodeError as e:
        print(f"[Gemini Traffic] JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"[Gemini Traffic ERROR] {e}")
        return None


# ── New API endpoint: /api/gemini-traffic ─────────────────────────────────────

@app.route("/api/gemini-traffic", methods=["POST"])
def api_gemini_traffic():
    """
    Called from the frontend AFTER route data is loaded, to fetch
    Gemini-powered real-time traffic + incidents for the corridor.
    Body: {origin, destination, origin_lat, origin_lng, dest_lat, dest_lng, dist_km}
    """
    body = request.json or {}
    origin      = body.get("origin", "").strip()
    destination = body.get("destination", "").strip()
    origin_lat  = float(body.get("origin_lat", 0))
    origin_lng  = float(body.get("origin_lng", 0))
    dest_lat    = float(body.get("dest_lat", 0))
    dest_lng    = float(body.get("dest_lng", 0))
    dist_km     = float(body.get("dist_km", 100))

    if not origin or not destination:
        return jsonify({"error": "origin and destination required"}), 400

    result = call_gemini_traffic(origin, destination,
                                  origin_lat, origin_lng,
                                  dest_lat, dest_lng, dist_km)
    if result:
        return jsonify({"success": True, "data": result})
    else:
        # Return a structured error so the frontend can show a fallback
        return jsonify({"success": False, "error": "Gemini unavailable"}), 200


# ══════════════════════════════════════════════════════════════════════════════
# Overpass helper
# ══════════════════════════════════════════════════════════════════════════════

def _overpass_post(query, timeout=22):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer":       "https://logiroute.app/",
        "User-Agent":    "LogisticsRoute/8.0 (logistics research)",
    }
    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers=headers,
            timeout=timeout+3,
        )
        if resp.status_code == 200:
            return resp.json().get("elements", [])
        if resp.status_code == 429:
            time.sleep(8)
            resp2 = requests.post(OVERPASS_URL, data={"data":query},
                                  headers=headers, timeout=timeout+3)
            if resp2.status_code == 200:
                return resp2.json().get("elements",[])
    except Exception as e:
        print(f"[Overpass ERROR] {e}")
    return []


# ── POI ────────────────────────────────────────────────────────────────────────

def get_pois_bulk(geo):
    if not geo:
        return {"garages":[], "refreshments":[]}

    s, w, n, e = route_bbox(geo, pad=0.3)
    bb = f"{s},{w},{n},{e}"

    cache_key = hashlib.md5(f"{round(s,1)}{round(w,1)}{round(n,1)}{round(e,1)}".encode()).hexdigest()
    if cache_key in _poi_cache:
        ts, cached = _poi_cache[cache_key]
        if time.time() - ts < POI_CACHE_TTL:
            return cached

    q_garage = f"""
[out:json][timeout:20];
(
  node["shop"~"car_repair|tyres|vehicle"]({bb});
  node["amenity"~"car_repair|vehicle_inspection"]({bb});
  way["shop"~"car_repair|tyres"]({bb});
  way["amenity"="car_repair"]({bb});
);
out center body 60;
"""
    q_food = f"""
[out:json][timeout:20];
(
  node["amenity"~"restaurant|fast_food|cafe|food_court"]({bb});
  node["amenity"="fuel"]({bb});
  node["shop"~"convenience|supermarket"]({bb});
  way["amenity"="fuel"]({bb});
);
out center body 100;
"""

    with ThreadPoolExecutor(max_workers=2) as ex:
        fg = ex.submit(_overpass_post, q_garage, 22)
        ff = ex.submit(_overpass_post, q_food,   22)
    garage_els = fg.result()
    food_els   = ff.result()

    pts    = sample_geo(geo, n=6)
    garages=[]; refreshments=[]
    seen_g=set(); seen_r=set()

    def closest(elat, elng):
        return round(min(haversine(elat,elng,p[0],p[1]) for p in pts),1) if pts else 0.0

    def get_pos(el):
        if "lat" in el: return el["lat"], el["lon"]
        c = el.get("center",{})
        return c.get("lat",0), c.get("lon",0)

    for el in garage_els:
        tags = el.get("tags",{})
        name = tags.get("name") or tags.get("brand") or tags.get("operator","")
        if not name: continue
        elat, elng = get_pos(el)
        if not elat: continue
        uid = f"{round(elat,3)},{round(elng,3)}"
        if uid in seen_g: continue
        seen_g.add(uid)
        shop    = tags.get("shop","")
        amenity = tags.get("amenity","")
        cat     = ("Tyre Shop"          if shop=="tyres"              else
                   "Vehicle Service"    if shop=="vehicle"            else
                   "Vehicle Inspection" if amenity=="vehicle_inspection" else
                   "Car Repair")
        addr = ", ".join(filter(None,[
            tags.get("addr:street",""), tags.get("addr:housenumber",""),
            tags.get("addr:city",""),   tags.get("addr:state","")]))
        garages.append({
            "lat":elat,"lng":elng,"name":name,
            "category":      cat,
            "address":       addr or "—",
            "phone":         tags.get("phone",tags.get("contact:phone","—")),
            "opening_hours": tags.get("opening_hours","—"),
            "website":       tags.get("website",tags.get("contact:website","")),
            "dist_km":       closest(elat,elng),
            "source":        "OpenStreetMap",
        })

    for el in food_els:
        tags    = el.get("tags",{})
        name    = tags.get("name") or tags.get("brand") or tags.get("operator","")
        if not name: continue
        elat, elng = get_pos(el)
        if not elat: continue
        uid = f"{round(elat,3)},{round(elng,3)}"
        if uid in seen_r: continue
        seen_r.add(uid)
        amenity = tags.get("amenity","")
        shop    = tags.get("shop","")
        addr    = ", ".join(filter(None,[
            tags.get("addr:street",""), tags.get("addr:housenumber",""),
            tags.get("addr:city",""),   tags.get("addr:state","")]))
        dist = closest(elat, elng)

        if amenity == "fuel":
            refreshments.append({
                "lat":elat,"lng":elng,"name":name,
                "category":"Fuel Station","icon_type":"fuel",
                "brand":tags.get("brand",""),
                "address":addr or "—","dist_km":dist,
                "source":"OpenStreetMap",
            })
            if tags.get("service:vehicle:repair")=="yes" and uid not in seen_g:
                seen_g.add(uid)
                garages.append({
                    "lat":elat,"lng":elng,"name":name,
                    "category":"Fuel + Repair",
                    "address":addr or "—",
                    "phone":tags.get("phone","—"),
                    "opening_hours":tags.get("opening_hours","—"),
                    "website":tags.get("website",""),
                    "dist_km":dist,"source":"OpenStreetMap",
                })
        elif amenity in ("restaurant","fast_food","cafe","food_court"):
            cat   = {"restaurant":"Restaurant","fast_food":"Fast Food",
                     "cafe":"Café","food_court":"Food Court"}.get(amenity,"Stop")
            itype = "cafe" if amenity=="cafe" else "restaurant"
            refreshments.append({
                "lat":elat,"lng":elng,"name":name,
                "category":cat,"icon_type":itype,
                "cuisine":tags.get("cuisine",""),
                "address":addr or "—","dist_km":dist,
                "source":"OpenStreetMap",
            })
        elif shop in ("convenience","supermarket"):
            refreshments.append({
                "lat":elat,"lng":elng,"name":name,
                "category":"Convenience Store","icon_type":"store",
                "address":addr or "—","dist_km":dist,
                "source":"OpenStreetMap",
            })

    garages.sort(key=lambda x: x["dist_km"])
    refreshments.sort(key=lambda x: x["dist_km"])
    result = {"garages":garages[:18], "refreshments":refreshments[:22]}
    _poi_cache[cache_key] = (time.time(), result)
    return result


# ── Incidents (OSM fallback) ───────────────────────────────────────────────────

def get_incidents(lat1, lng1, lat2, lng2):
    s=min(lat1,lat2)-0.4; n=max(lat1,lat2)+0.4
    w=min(lng1,lng2)-0.4; e=max(lng1,lng2)+0.4
    bb = f"{s},{w},{n},{e}"
    q  = f"""
[out:json][timeout:12];
(
  way["highway"="construction"]({bb});
  way["construction"]["highway"]({bb});
);
out center 20;
"""
    els = _overpass_post(q, timeout=14)
    out = []
    for el in els[:12]:
        tags = el.get("tags",{}); ctr = el.get("center",{})
        if not ctr: continue
        out.append({
            "lat":ctr.get("lat",0),"lng":ctr.get("lon",0),
            "type":"Road Works","severity":"Moderate","delay_min":12,
            "description":f"{tags.get('name',tags.get('ref','Road segment'))} — construction",
            "road":tags.get("ref",""),"source":"OpenStreetMap",
        })
    return out


# ── Risk & parse ───────────────────────────────────────────────────────────────

def check_accident_zones(geo):
    hit=[]; sample=geo[::10] if len(geo)>10 else geo
    for zone in ACCIDENT_ZONES:
        for pt in sample:
            if haversine(pt[0],pt[1],zone["lat"],zone["lng"])<=zone["radius_km"]:
                if zone["name"] not in [z["name"] for z in hit]: hit.append(zone)
                break
    return hit

def compute_risk(dist, dur, wo, wd, wm, td, road, az):
    bd={}
    bd["weather"] =round(((wo["risk_score"]+wd["risk_score"]+wm["risk_score"])/3/5)*FACTOR_WEIGHTS["weather"],3)
    bd["accident"]=round(min(len(az)*0.5,1.0)*FACTOR_WEIGHTS["accident"],3)
    bd["traffic"] =round(td.get("congestion_index",0.3)*FACTOR_WEIGHTS["traffic"],3)
    bd["road"]    =round({"Good":0.1,"Fair":0.5,"Poor":1.0}.get(road,0.3)*FACTOR_WEIGHTS["road"],3)
    bd["distance"]=round((1.0 if dist>700 else 0.7 if dist>400 else 0.45 if dist>200 else 0.25 if dist>100 else 0.1)*FACTOR_WEIGHTS["distance"],3)
    bd["duration"]=round((1.0 if dur>600 else 0.7 if dur>360 else 0.4 if dur>180 else 0.2 if dur>90 else 0.1)*FACTOR_WEIGHTS["duration"],3)
    return bd, round(min(sum(bd.values()),10.0),1)

def parse_route(raw, idx, wo, wd, wm):
    summ    = raw.get("summary",{})
    dist_km = round(summ.get("distance",0),2)
    dur_min = round(summ.get("duration",0)/60,1)
    geo     = geo_from_raw(raw)
    steps   = [{"instruction":s.get("instruction",""),
                "distance":round(s.get("distance",0),2),
                "duration":round(s.get("duration",0)/60,1)}
               for seg in raw.get("segments",[]) for s in seg.get("steps",[])]
    td      = compute_traffic(geo, dist_km, dur_min, idx)
    road    = random.Random(idx*99+int(dist_km)).choice(["Good","Good","Good","Fair","Fair","Poor"])
    az      = check_accident_zones(geo)
    bd,risk = compute_risk(dist_km,dur_min,wo,wd,wm,td,road,az)
    rl      = ("Low" if risk<=2.5 else "Moderate" if risk<=5 else "High" if risk<=7.5 else "Critical")
    return {
        "id":f"route_{idx}","mode_label":f"Route {idx+1}",
        "color":ROUTE_COLORS[idx%len(ROUTE_COLORS)],
        "distance_km":dist_km,"duration_min":dur_min,
        "geometry":geo,"steps":steps[:30],
        "risk_score":risk,"risk_label":rl,"risk_breakdown":bd,
        "traffic":td.get("level","Unknown"),"traffic_data":td,
        "road_condition":road,"accident_zones":[z["name"] for z in az],
        "weather":{"origin":wo,"midpoint":wm,"destination":wd},
        "is_fastest":False,"is_safest":False,"is_recommended":False,
    }


# ── Gemini AI Advisory ────────────────────────────────────────────────────────

def _build_gemini_prompt(routes, origin_label, dest_label):
    ranked = sorted(routes, key=lambda r: (r["risk_score"], r["duration_min"]))
    route_summaries = []
    for i, r in enumerate(ranked):
        tt  = r.get("traffic_data", {})
        wx  = r.get("weather", {})
        bd  = r.get("risk_breakdown", {})
        route_summaries.append({
            "rank":i+1,"id":r["id"],"label":r["mode_label"],
            "distance_km":r["distance_km"],"duration_min":r["duration_min"],
            "risk_score":r["risk_score"],"risk_label":r["risk_label"],
            "risk_breakdown":{k:round(v,2) for k,v in bd.items()},
            "traffic_level":tt.get("level","Unknown"),
            "avg_speed_kmh":tt.get("avg_speed_kmh",0),
            "delay_min":tt.get("delay_min",0),
            "road_condition":r.get("road_condition","Unknown"),
            "accident_zones":r.get("accident_zones",[]),
            "weather_origin":{
                "condition":wx.get("origin",{}).get("condition","Unknown"),
                "temp_c":wx.get("origin",{}).get("temp_c",0),
                "wind_kmh":wx.get("origin",{}).get("wind_kmh",0),
                "visibility_km":wx.get("origin",{}).get("visibility_km",10),
                "risk_score":wx.get("origin",{}).get("risk_score",0),
            },
            "weather_dest":{
                "condition":wx.get("destination",{}).get("condition","Unknown"),
                "temp_c":wx.get("destination",{}).get("temp_c",0),
                "risk_score":wx.get("destination",{}).get("risk_score",0),
            },
        })

    return f"""You are LogiRoute — an AI logistics safety advisor for Indian freight operations.

ROUTE REQUEST
Origin      : {origin_label}
Destination : {dest_label}
Routes found: {len(routes)}

ROUTE DATA (ranked safest first)
{json.dumps(route_summaries, indent=2)}

TASK
Analyze the routes above and return a JSON object with EXACTLY these keys:

{{
  "verdict":       "<one sentence — which route is recommended and why>",
  "explanation":   "<2-3 sentences — traffic, weather, road condition, distance trade-offs>",
  "warnings":      ["<up to 4 specific hazard warnings>"],
  "avoid_reason":  "<one sentence — which route to avoid and the exact reason>",
  "cargo_tip":     "<one practical cargo handling tip>",
  "confidence":    "<High|Medium|Low>"
}}

RULES
- Return ONLY the JSON object. No markdown, no backticks.
- warnings must be a JSON array of strings.
"""


def _call_gemini(prompt):
    if not GEMINI_KEY: return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature":0.3,"maxOutputTokens":600,"topP":0.8},
    }
    try:
        resp = requests.post(f"{GEMINI_URL}?key={GEMINI_KEY}", json=payload,
                             headers={"Content-Type":"application/json"}, timeout=15)
        if resp.status_code != 200: return None
        raw_text = (resp.json().get("candidates",[{}])[0]
                    .get("content",{}).get("parts",[{}])[0].get("text",""))
        clean = re.sub(r"```(?:json)?|```","",raw_text).strip()
        return json.loads(clean)
    except Exception as e:
        print(f"[Gemini ERROR] {e}")
        return None


def _static_ai_rec(routes):
    ranked = sorted(routes, key=lambda r: (r["risk_score"], r["duration_min"]))
    best   = ranked[0]; worst = ranked[-1]
    gap    = round(worst["risk_score"]-best["risk_score"],1)
    bd     = best["risk_breakdown"]; tt = best.get("traffic_data",{})
    warns  = []
    if bd.get("weather",0)>=1.5:  warns.append(f"Weather: {best['weather']['origin']['condition']} at origin")
    if bd.get("accident",0)>=1.5: warns.append(f"{len(best['accident_zones'])} accident zone(s) on route")
    if tt.get("level") in ("Heavy","Standstill"): warns.append(f"Traffic: {tt['level']} ({tt.get('avg_speed_kmh',0)} km/h)")
    if bd.get("road",0)>=0.75:    warns.append(f"Road: {best['road_condition']} — secure cargo")
    return {
        "verdict":f"{best['mode_label']} is safest — Risk {best['risk_score']}/10 ({gap} pts safer than riskiest)",
        "explanation":f"Ranked {len(routes)} routes. Traffic: {tt.get('level','—')} · {best['distance_km']} km · {best['duration_min']} min · Road: {best['road_condition']}.",
        "warnings":warns[:4],
        "avoid_reason":f"{worst['mode_label']} — highest risk ({worst['risk_score']}/10): {worst['traffic']} traffic · {worst['road_condition']} roads.",
        "cargo_tip":"","confidence":"N/A","source":"rule-based",
    }


def ai_rec(routes, origin_label="", dest_label=""):
    if not routes: return {}
    ranked     = sorted(routes, key=lambda r: (r["risk_score"], r["duration_min"]))
    best       = ranked[0]; worst = ranked[-1]
    ranked_ids = [r["id"] for r in ranked]

    gemini_result = None
    if GEMINI_KEY:
        prompt = _build_gemini_prompt(routes, origin_label, dest_label)
        gemini_result = _call_gemini(prompt)

    if gemini_result:
        warnings = gemini_result.get("warnings",[])
        if isinstance(warnings, str): warnings = [warnings]
        return {
            "recommended_id":best["id"],"ranked_ids":ranked_ids,
            "verdict":gemini_result.get("verdict",""),
            "explanation":gemini_result.get("explanation",""),
            "warnings":warnings[:4],
            "avoid_reason":gemini_result.get("avoid_reason",""),
            "cargo_tip":gemini_result.get("cargo_tip",""),
            "confidence":gemini_result.get("confidence",""),
            "source":"gemini-2.0-flash",
        }

    fb = _static_ai_rec(routes)
    return {"recommended_id":best["id"],"ranked_ids":ranked_ids,**fb}


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static","index.html")

@app.route("/api/routes", methods=["POST"])
def api_routes():
    body   = request.json or {}
    origin = body.get("start","").strip()
    dest   = body.get("end","").strip()
    if not origin or not dest:
        return jsonify({"error":"Both 'start' and 'end' are required."}),400

    t_total = time.time()

    with ThreadPoolExecutor(max_workers=2) as ex:
        fsc=ex.submit(geocode,origin); fec=ex.submit(geocode,dest)
    sc=fsc.result(); ec=fec.result()
    if not sc: return jsonify({"error":f"Could not geocode: '{origin}'"}),400
    if not ec: return jsonify({"error":f"Could not geocode: '{dest}'"}),400

    mid_lat=(sc["lat"]+ec["lat"])/2; mid_lng=(sc["lng"]+ec["lng"])/2

    with ThreadPoolExecutor(max_workers=2) as ex:
        fwx=ex.submit(fetch_weather_all,sc,ec,mid_lat,mid_lng)
        frt=ex.submit(collect_routes,sc,ec)
    wo,wd,wm=fwx.result(); raw_collected=frt.result()

    if not raw_collected:
        return jsonify({"error":"No routes found. Check ORS API key."}),404

    all_routes=[parse_route(raw,i,wo,wd,wm) for i,raw in enumerate(raw_collected)]
    by_time=sorted(all_routes,key=lambda r:r["duration_min"])
    by_time[0]["is_fastest"]=True
    safest=min(all_routes,key=lambda r:r["risk_score"])
    safest["is_safest"]=True
    ai=ai_rec(all_routes, sc.get("label",""), ec.get("label",""))
    for r in all_routes:
        if r["id"]==ai.get("recommended_id"): r["is_recommended"]=True
    all_routes.sort(key=lambda r:(r["risk_score"],r["duration_min"]))

    best_geo=all_routes[0]["geometry"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        fpoi=ex.submit(get_pois_bulk,best_geo)
        finc=ex.submit(get_incidents,sc["lat"],sc["lng"],ec["lat"],ec["lng"])
    pois=fpoi.result(); incidents=finc.result()

    elapsed=round(time.time()-t_total,1)
    print(f"[Done] {len(all_routes)} routes | {elapsed}s")

    return jsonify({
        "start":sc,"end":ec,
        "routes":all_routes,
        "fastest":by_time[0],"safest":safest,
        "ai_recommendation":ai,
        "total":len(all_routes),
        "pois":pois,
        "incidents":incidents,
        "corridor_traffic":all_routes[0].get("traffic_data",{}),
    })

@app.route("/api/traffic/now", methods=["GET"])
def api_traffic_now():
    f=time_cong(); h=(datetime.utcnow().hour+5)%24
    lv=("Free Flow" if f<0.14 else "Light"  if f<0.33 else
        "Moderate"  if f<0.56 else "Heavy"  if f<0.76 else "Standstill")
    pk=("Morning Rush" if 7<=h<=10 else "Evening Rush" if 16<=h<=20 else "Off-Peak")
    return jsonify({"congestion_factor":round(f,2),"level":lv,
                    "hour_ist":h,"period":pk,
                    "updated_at":datetime.utcnow().strftime("%H:%M UTC")})

if __name__ == "__main__":
    app.run(debug=True, port=5000)