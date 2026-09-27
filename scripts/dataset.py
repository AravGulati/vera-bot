"""Load the expanded challenge dataset (50 merchants / 200 customers / 100 triggers)."""
import json, glob, os
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset", "expanded")

def load():
    cats = {json.load(open(f))["slug"]: json.load(open(f)) for f in glob.glob(f"{ROOT}/categories/*.json")}
    ms = {json.load(open(f))["merchant_id"]: json.load(open(f)) for f in glob.glob(f"{ROOT}/merchants/*.json")}
    cs = {json.load(open(f))["customer_id"]: json.load(open(f)) for f in glob.glob(f"{ROOT}/customers/*.json")}
    ts = {json.load(open(f))["id"]: json.load(open(f)) for f in glob.glob(f"{ROOT}/triggers/*.json")}
    pairs = json.load(open(f"{ROOT}/test_pairs.json"))["pairs"]
    return cats, ms, cs, ts, pairs
