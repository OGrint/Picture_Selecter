"""One-off: map GBIF's country list to iNaturalist country place ids (admin_level 0)."""
import json
import sys
import time
import urllib.parse
import urllib.request

src, out = sys.argv[1], sys.argv[2]
countries = json.load(open(src, encoding="utf-8"))
UA = {"User-Agent": "PhotoReviewer/1.0"}


def lookup(q: str):
    url = "https://api.inaturalist.org/v1/places/autocomplete?" + urllib.parse.urlencode({"q": q, "per_page": 10})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        res = json.loads(r.read())["results"]
    return [p for p in res if p.get("admin_level") == 0]


rows, misses = [], []
for c in countries:
    if c["iso2"] in ("ZZ", "XK") and c["title"].lower().startswith("unknown"):
        continue
    names = [c["title"], c["title"].split(",")[0], c["enumName"].replace("_", " ").title()]
    hit = None
    for q in dict.fromkeys(names):
        try:
            cands = lookup(q)
        except Exception as e:
            print("error", q, e, flush=True)
            cands = []
        time.sleep(1.0)
        exact = [p for p in cands if p["name"].lower() == q.lower()]
        if exact or cands:
            hit = (exact or cands)[0]
            break
    if hit:
        rows.append({"iso": c["iso2"], "name": c["title"], "place_id": hit["id"],
                     "inat_name": hit["name"], "region": c.get("gbifRegion", "")})
    else:
        misses.append(c["title"])
    print(len(rows), c["title"], "->", hit["name"] if hit else "MISS", flush=True)
json.dump({"countries": rows}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print("done", len(rows), "matched; misses:", misses)
