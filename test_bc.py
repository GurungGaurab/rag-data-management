"""Tests B and C for pipeline.py.

B: a changed file with the same name gets its own outputs; putting the original back is skipped.
C: a broken PDF is recorded as failed and the run carries on; a missing Gemini key gives a clear
   "not read" reason instead of a crash.

Everything happens on copies in data/test_bc/, so data/raw, your real runs and your .env are
not touched. Run:  python test_bc.py
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

import pipeline as P

ROOT = Path(__file__).parent
BASE = ROOT / "data" / "test_bc"
ORIG_2025 = ROOT / "data" / "raw" / "handbook_2025.pdf"
ORIG_2026 = ROOT / "data" / "raw" / "handbook_2026.pdf"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


def md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def run(raw_dir, out_dir, do_ocr=False, use_gemini=False, page_range=(6, 8)):
    """Run pipeline.main() on a test folder. Pages 6-8 only, to keep it quick."""
    P.RAW_DIR, P.OUT_DIR, P.MANIFEST = raw_dir, out_dir, out_dir / "manifest.json"
    P.DO_OCR, P.USE_GEMINI, P.PAGE_RANGE = do_ocr, use_gemini, page_range
    P.main()
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))["files"]


shutil.rmtree(BASE, ignore_errors=True)

# ---------------------------------------------------------------- Test B
print("\n################ TEST B: changed file, same name ################")
raw, out = BASE / "B_raw", BASE / "B_out"
raw.mkdir(parents=True)
target = raw / "handbook_2025.pdf"

print("\n--- B1: first run, original 2025 content")
shutil.copy(ORIG_2025, target)
hash_a = P.sha_256(target)[:8]
files = run(raw, out)
json_a = out / f"handbook_2025_{hash_a}.json"
check("B1 parsed, output named with the hash", files[0]["status"] == "success" and json_a.exists(), json_a.name)
md5_a = md5(json_a) if json_a.exists() else None

print("\n--- B2: same name, different content (2026 PDF copied over it)")
shutil.copy(ORIG_2026, target)
hash_b = P.sha_256(target)[:8]
files = run(raw, out)
json_b = out / f"handbook_2025_{hash_b}.json"
check("B2 changed file parsed again", files[0]["status"] == "success", f"new hash {hash_b}")
check("B2 new version has its own output", json_b.exists(), json_b.name)
check("B2 old version's output untouched", json_a.exists() and md5(json_a) == md5_a, json_a.name)

print("\n--- B3: original content put back")
shutil.copy(ORIG_2025, target)
files = run(raw, out)
check("B3 original skipped as already parsed", files[0]["status"] == "skipped", files[0].get("reason", ""))

# ---------------------------------------------------------------- Test C
print("\n################ TEST C: broken PDF + missing Gemini key ################")
raw, out = BASE / "C_raw", BASE / "C_out"
raw.mkdir(parents=True)
(raw / "broken.pdf").write_text("this is not a PDF")
shutil.copy(ORIG_2026, raw / "handbook_2026.pdf")

# Simulate a missing key without touching your .env: look for .env in the test folder instead,
# and hide any key set in this script's environment.
P.ROOT = BASE
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)

files = run(raw, out, do_ocr=True, use_gemini=True)
by_file = {f["file"]: f for f in files}

broken = by_file.get("broken.pdf", {})
check("C broken PDF recorded as failed", broken.get("status") in ("failed", "failure"),
      (broken.get("error") or str(broken.get("errors", "")))[:100])

good = by_file.get("handbook_2026.pdf", {})
check("C other PDF still processed", good.get("status") == "success", good.get("status", ""))

images = good.get("embedded_images", [])
kept_ocr = [i for i in images if i["source"] == "ocr"]
needed_gemini = [i for i in images if i["ocr_check"] not in ("passed", "image too small, not read")]
check("C good OCR image still read by OCR", len(kept_ocr) > 0, f"{len(kept_ocr)} image(s)")
check("C images needing Gemini are 'not read' with a clear reason",
      len(needed_gemini) > 0 and all(i["status"] == "not read" and "GEMINI_API_KEY" in (i["gemini_error"] or "")
                                     for i in needed_gemini),
      "; ".join(f"p{i['page']}: {i['gemini_error']}" for i in needed_gemini))

# ---------------------------------------------------------------- Summary
print(f"\n################ {sum(results)} of {len(results)} checks passed ################")
print(f"Test files are in {BASE} (inside data/, so not committed). You can delete that folder afterwards.")
