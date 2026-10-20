#!/usr/bin/env python3
"""Download tsunami evacuation data for offline use: Washington DNR's hazard area, evacuation routes, assembly areas,
vertical evacuation structures and sirens, and Oregon DOGAMI's local (Cascadia) and distant evacuation zones.

    fetch_emergency.py [OUTDIR]     (default ~/.local/share/headunit/emergency)

Writes zones.geojson, routes.geojson, assembly.geojson and sirens.geojson (WGS84, polygons simplified to ~10 m).
Run it again to refresh; the agencies update these every few years. Stdlib only.
"""
import json
import pathlib
import sys
import urllib.parse
import urllib.request

USER_AGENT = "nomad-headunit/0.1 (offline emergency maps)"
WA = "https://gis.dnr.wa.gov/site3/rest/services/Geology/Tsunami_Hazard/FeatureServer"
OR = "https://gis.dogami.oregon.gov/arcgis/rest/services/StoryMaps/Tsunami_Evacuation_Zones_Local/MapServer/0"


def query(layer, where="1=1", simplify=None):
    """Every feature of an ArcGIS layer as GeoJSON features, a page at a time."""
    feats, offset = [], 0
    while True:
        params = {"where": where, "outFields": "*", "outSR": 4326, "f": "geojson",
                  "resultOffset": offset, "resultRecordCount": 500}
        if simplify:
            params["maxAllowableOffset"] = simplify
        req = urllib.request.Request(f"{layer}/query?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            page = json.load(r)
        got = page.get("features") or []
        feats += got
        if len(got) < 500 and not page.get("exceededTransferLimit"):
            return feats
        offset += len(got)


def props(f, **keep):
    """The feature with only the properties we use, renamed: keep maps new name -> source name or constant."""
    src = f.get("properties") or {}
    return {"type": "Feature", "geometry": f["geometry"],
            "properties": {k: (src.get(v) if isinstance(v, str) and v in src else v) for k, v in keep.items()}}


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / ".local/share/headunit/emergency")
    out.mkdir(parents=True, exist_ok=True)
    zones = [props(f, state="WA", zone="hazard") for f in query(f"{WA}/36", simplify=0.0001)]
    print(f"WA hazard area: {len(zones)}", flush=True)
    for kind, label in (("local", "Local Tsunami Evacuation Zone"), ("distant", "Distant Tsunami Evacuation Zone")):
        got = [props(f, state="OR", zone=kind) for f in query(OR, f"Type = '{label}'", simplify=0.0001)]
        print(f"OR {kind} zones: {len(got)}", flush=True)
        zones += got
    sets = {
        "zones": zones,
        "routes": [props(f, state="WA") for f in query(f"{WA}/20", simplify=0.00005)],
        "assembly": [props(f, state="WA", kind="SITE_TYPE", name="SITE_NAME", address="SITE_DESCRIPTION",
                           elev_ft="ELEVATION_FT") for f in query(f"{WA}/26")]
                    + [props(f, state="WA", kind="Vertical evacuation structure", name="SITE_NAME",
                             address="SITE_DESCRIPTION", elev_ft="ELEVATION_FT") for f in query(f"{WA}/23")],
        "sirens": [props(f, state="WA") for f in query(f"{WA}/22")],
    }
    for name, feats in sets.items():
        feats = [f for f in feats if f.get("geometry")]
        path = out / f"{name}.geojson"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        tmp.replace(path)
        print(f"{name}: {len(feats)} features, {path.stat().st_size / 1e6:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
