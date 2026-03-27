#!/usr/bin/env python3
"""
SeaRoute Maritime Routing Server
Deploy to Render.com (free, no credit card) for public access.
"""
import math, os
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=".")
ENGINE = None
GRAPH  = None

def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0; r = math.pi / 180
    dlat=(lat2-lat1)*r; dlon=(lon2-lon1)*r
    a=math.sin(dlat/2)**2+math.cos(lat1*r)*math.cos(lat2*r)*math.sin(dlon/2)**2
    return R*2*math.asin(math.sqrt(max(0,a)))

def load_engine():
    global ENGINE, GRAPH
    try:
        from scgraph.geographs.marnet import marnet_geograph
        GRAPH=marnet_geograph; ENGINE='scgraph'
        print(f"  Engine: scgraph — {len(GRAPH.graph)} nodes"); return True
    except: pass
    try:
        import searoute as sr
        GRAPH=sr; ENGINE='searoute'
        print("  Engine: searoute-py"); return True
    except: pass
    print("  ERROR: pip install searoute"); return False

PASSAGE_MAP = {
    'suez':'suez','panama':'panama','malacca':'malacca',
    'gibraltar':'gibraltar','babalmandab':'babalmandab',
    'northwest':'northwest','northeast':'northeast',
    'magellan':'chili','sunda':'sunda','ormuz':'ormuz','kiel':'kiel',
}

def detect_passages(coords):
    passages=[]
    for lon,lat in coords:
        if 32.0<lon<33.0 and 29.5<lat<31.5:
            if 'suez' not in passages: passages.append('suez')
        if -80.0<lon<-79.0 and 8.7<lat<9.5:
            if 'panama' not in passages: passages.append('panama')
        if 99.0<lon<104.0 and 1.0<lat<6.0:
            if 'malacca' not in passages: passages.append('malacca')
        if -6.0<lon<-5.0 and 35.7<lat<36.2:
            if 'gibraltar' not in passages: passages.append('gibraltar')
        if 43.0<lon<44.0 and 12.0<lat<13.0:
            if 'babalmandab' not in passages: passages.append('babalmandab')
    return passages

def name_from_passages(passages):
    if not passages: return "OPEN OCEAN"
    p=[x.lower() for x in passages]
    if 'suez' in p: return "VIA SUEZ CANAL"
    if 'panama' in p: return "VIA PANAMA CANAL"
    if 'chili' in p: return "VIA STRAIT OF MAGELLAN"
    if 'northwest' in p: return "VIA NORTHWEST PASSAGE"
    return "VIA "+" & ".join([x.upper() for x in p])

def route_scgraph(olon,olat,dlon,dlat,restrictions):
    try:
        result=GRAPH.get_shortest_path(
            origin_node={"latitude":olat,"longitude":olon},
            destination_node={"latitude":dlat,"longitude":dlon},
            output_units='km', node_addition_type='quadrant',
            destination_node_addition_type='all',
        )
        if not result or 'coordinate_path' not in result:
            return {"error":"No route found"}
        coords=[[c['longitude'],c['latitude']] for c in result['coordinate_path']]
        total_km=result.get('length',0)
        passages=detect_passages(coords)
        return {"coordinates":coords,"distance_km":round(total_km,1),
                "distance_nm":round(total_km/1.852,1),
                "route_name":name_from_passages(passages),
                "passages":passages,"node_count":len(coords),"warning":None}
    except Exception as e:
        return {"error":str(e)}

def route_searoute(olon,olat,dlon,dlat,restrictions):
    import searoute as sr
    sr_r=[PASSAGE_MAP[r] for r in restrictions if r in PASSAGE_MAP]

    try:
        route=sr.searoute([olon,olat],[dlon,dlat],units="km",
                          append_orig_dest=True,restrictions=sr_r,return_passages=True)
    except Exception as e:
        return {"error":str(e)}
    if not route: return {"error":"No route found"}

    geom=route.get("geometry",{}); coords=geom.get("coordinates",[])
    props=route.get("properties",{}); total_km=props.get("length",0)
    passages=props.get("passages",[])
    if isinstance(passages,str): passages=[passages] if passages else []

    # ── POST-PROCESS: fix known land-crossing segments ──────────────────────
    # Strategy: scan consecutive coordinate pairs for segments that pass through
    # known land-crossing zones, and replace them with safe open-water waypoints.
    # This does NOT add routing legs — it only adjusts display coordinates.
    # The distance from searoute is kept as-is (it's calculated on the graph,
    # not on the display polyline, so it's already correct).
    coords = fix_land_crossings(coords)

    return {"coordinates":coords,"distance_km":round(total_km,1),
            "distance_nm":round(total_km/1.852,1),
            "route_name":name_from_passages(passages),
            "passages":passages,"node_count":len(coords),"warning":None}


def segment_crosses_box(lon1,lat1,lon2,lat2, box_lon1,box_lat1,box_lon2,box_lat2):
    """Check if a line segment passes through a bounding box."""
    # Quick check: does the segment's bounding box overlap the hazard box?
    seg_min_lon=min(lon1,lon2); seg_max_lon=max(lon1,lon2)
    seg_min_lat=min(lat1,lat2); seg_max_lat=max(lat1,lat2)
    if seg_max_lon<box_lon1 or seg_min_lon>box_lon2: return False
    if seg_max_lat<box_lat1 or seg_min_lat>box_lat2: return False
    return True


def fix_land_crossings(coords):
    """
    Post-process coordinate list to fix known MARNET land-crossing artifacts.
    Hazard zones and their safe replacement waypoint sequences.
    Each hazard has: bounding box + list of open-water waypoints to insert
    when a segment crosses that box.
    """
    # Known MARNET hazard zones: [lon_min, lat_min, lon_max, lat_max, [[safe waypoints]]]
    HAZARDS = [
        # Gulf of Aden / Socotra — route clips over island or Yemen coast
        # Safe corridor: stay between 11.5°N–13°N threading south of Yemen peninsula
        {
            'box': [43.0, 11.0, 57.0, 14.5],
            'safe': [
                [43.5, 12.3],   # just past Bab-el-Mandeb, open water
                [45.5, 12.0],   # Gulf of Aden centre
                [48.0, 11.8],   # Gulf of Aden E, south of Yemen tip
                [51.0, 11.5],   # clear of Socotra (south)
                [54.0, 12.0],   # E. Gulf of Aden, open water
            ]
        },
        # Red Sea northern exit — sometimes clips Sinai/Egypt
        {
            'box': [31.5, 27.5, 34.5, 30.5],
            'safe': [
                [32.5, 29.5],   # centre Red Sea N
            ]
        },
    ]

    if len(coords) < 2:
        return coords

    new_coords = [coords[0]]

    for i in range(len(coords)-1):
        lon1,lat1 = coords[i]
        lon2,lat2 = coords[i+1]

        inserted = False
        for hz in HAZARDS:
            b = hz['box']
            if segment_crosses_box(lon1,lat1,lon2,lat2, b[0],b[1],b[2],b[3]):
                # Check if either endpoint is already inside the safe zone
                # (avoid inserting if segment is just traversing normally)
                mid_lon = (lon1+lon2)/2
                mid_lat = (lat1+lat2)/2
                # Only insert waypoints if midpoint is IN the hazard box
                if b[0] <= mid_lon <= b[2] and b[1] <= mid_lat <= b[3]:
                    for wp in hz['safe']:
                        new_coords.append(wp)
                    inserted = True
                    break

        new_coords.append([lon2, lat2])

    return new_coords


@app.route("/api/status")
def status():
    r=jsonify({"status":"ready" if ENGINE else "unavailable",
               "engine":ENGINE or "none",
               "nodes":len(GRAPH.graph) if ENGINE=='scgraph' else 0,
               "backend":"scgraph (MARNET)" if ENGINE=='scgraph' else ("searoute-py" if ENGINE=='searoute' else "not loaded")})
    r.headers["Access-Control-Allow-Origin"]="*"; return r

@app.route("/api/route")
def route_api():
    if not ENGINE: return jsonify({"error":"Engine not loaded"}),503
    try:
        olon=float(request.args["olon"]); olat=float(request.args["olat"])
        dlon=float(request.args["dlon"]); dlat=float(request.args["dlat"])
    except: return jsonify({"error":"Required: olon,olat,dlon,dlat"}),400
    restrictions=[x.strip().lower() for x in request.args.get("avoid","").split(",") if x.strip()]
    result=route_scgraph(olon,olat,dlon,dlat,restrictions) if ENGINE=='scgraph' \
           else route_searoute(olon,olat,dlon,dlat,restrictions)
    r=jsonify(result); r.headers["Access-Control-Allow-Origin"]="*"
    return r,(400 if "error" in result else 200)

@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent,"index.html")

@app.route("/<path:f>")
def static_f(f):
    return send_from_directory(Path(__file__).parent,f)

load_engine()

if __name__=="__main__":
    port=int(os.environ.get("PORT",5050))
    print(f"\n  SeaRoute — open http://localhost:{port}\n")
    app.run(host="0.0.0.0",port=port,debug=False)
