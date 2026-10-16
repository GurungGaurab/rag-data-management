"""Docling stage: parse every PDF in data/raw and report what was read.

Run:     python pipeline.py
Output:  data/parsed/<RUN_NAME>/   (<name> = file name + first 8 characters of its SHA-256)
           <name>.json                  full DoclingDocument (the source for chunking)
           <name>.md                    body text; each picture labelled embedded / not matched
           <name>_p<N>_stored_<i>.png   stored (embedded) images, if SAVE_IMAGES
           <name>_p<N>_stored_<i>.txt   text read from that image (by OCR or Gemini), if any
           manifest.json                sha256 -> result; files already parsed are skipped
           report.json                  settings, versions and one record per PDF
"""

import hashlib
import html
import io
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pypdfium2 as pdfium
from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions, TableFormerMode, TableStructureOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ContentLayer, PictureItem



RUN_NAME = "pipeline_v3_gemini"
DO_OCR = True                          # True = Apple OCR (ocrmac, macOS only) reads text inside images; use a new RUN_NAME
TABLE_MODE = TableFormerMode.ACCURATE
PAGE_RANGE = None                       # e.g. (1, 8) for a quick test; None = whole file
SAVE_IMAGES = True                      # write each stored image as a PNG into the run folder

IMAGE_ONLY_SHARE = 0.3                  # stored image covers more than 30% of the page...
IMAGE_ONLY_WORDS = 30                   # ...and the page's own text layer has fewer than 30 words -> image-only page
FLAG_MISSING = 0.05                     # flag a page if more than 5% of its text-layer words are missing...
MIN_MISSING = 3                         # ...and at least 3 words are missing (avoids noise on near-empty pages)

# Reading the text inside stored images: OCR first, Gemini only when the OCR result looks poor
USE_GEMINI = True                      # True = send images that fail the OCR checks to Gemini (needs GEMINI_API_KEY in .env)
GEMINI_MODEL = "gemini-3.5-flash-lite"  # a free Flash model; check the name in AI Studio
CONFIDENCE_MIN = 0.95                   # OCR is trusted only if its average confidence is at least this...
REAL_WORD_MIN = 0.95                    # ...and at least this share of its words are real words
REAL_WORD_ZIPF = 3.0                    # a word counts as real if it is common English (wordfreq) or appears in the PDF's own text
MIN_IMAGE_SHARE = 0.02                  # images smaller than 2% of the page are not read (icons, bullets)
GEMINI_PROMPT = (
    "Transcribe this image for a document search system. Each line you output may be read on its own, "
    "so it must make sense without the lines around it.\n"
    "\n"
    "Rules:\n"
    "- Copy all text exactly. Keep headings as headings and bullet points as bullet points.\n"
    "- Every line or table row must be self-contained: include the context needed to understand it, "
    "such as the full date (day, month and year), or the row and column headings it belongs to.\n"
    "- If the image is a table, grid, timetable or schedule, read it cell by cell using its row and column "
    "headings. Output only cells that contain content; skip empty cells.\n"
    "- Labels that only mark position (week numbers, row numbers, page numbers) are context, not content: "
    "attach them to the entries they belong to instead of listing them alone.\n"
    "- If it is a chart or diagram, give its title, then the values or labels it shows.\n"
    "- If it is a photo, logo or decoration with no useful text, reply only: NO_TEXT\n"
    "- Never add anything that is not in the image. If something cannot be read, write [unreadable]."
)

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "data" / "parsed" / RUN_NAME
MANIFEST = OUT_DIR / "manifest.json"

ALL_LAYERS = {ContentLayer.BODY, ContentLayer.FURNITURE}
WORD = re.compile(r"\w+")
PLACEHOLDER = "<!-- picture -->"        # temporary marker, replaced by a label per picture

# "UPDATED AS OF 10TH JANUARY 2026", "10 TH JANUARY 2026", "Updated as of 13 May 2026"
DATE_PATTERN = re.compile(
    r"updated as of\s+(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})",
    re.IGNORECASE,
)

#Used to decide if the file has already been parsed or not
def sha_256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def words(text): #word count 
    return Counter(WORD.findall(html.unescape(text or "").lower()))


def pages_of(item):# every page an item sits on (one paragraph can cross a page break)
    """Every page an item sits on. One paragraph can cross a page break."""
    return sorted({p.page_no for p in item.prov})


#Docling 
def build_converter():
    options = PdfPipelineOptions(
        do_ocr=DO_OCR,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(mode=TABLE_MODE),
        generate_parsed_pages=True,     # keep bitmap_resources (stored images) after parsing
    )
    if DO_OCR:
        options.ocr_options = OcrMacOptions()   # reads layout regions with no PDF text or with an image
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=options,
            # default docling-parse backend; also decode the stored images' pixels
            backend_options=ThreadedDoclingParseBackendOptions(include_bitmap_images=True),
        )
    })


#Embedded images 
def pdf_text_layer(pdf_path):
    """The PDF's own text layer, read with pypdfium2 (so OCR text is never included).
    Returns (every word as a set, number of words per page)."""
    vocabulary = set()
    page_words = Counter()
    pdf = pdfium.PdfDocument(str(pdf_path))
    for i in range(len(pdf)):
        found = WORD.findall(pdf[i].get_textpage().get_text_range())
        vocabulary.update(w.lower() for w in found)
        page_words[i + 1] = len(found)
    pdf.close()
    return vocabulary, page_words


def real_word_share(text, vocabulary):
    """Share of words (3+ letters) that are common English or appear in the PDF's own text."""
    try:
        from wordfreq import zipf_frequency
    except ImportError:
        raise RuntimeError("DO_OCR needs the wordfreq package: pip install wordfreq")
    candidates = [w.lower() for w in re.findall(r"[A-Za-z]+", text) if len(w) >= 3]
    if not candidates:
        return None
    real = sum(1 for w in candidates if w in vocabulary or zipf_frequency(w, "en") >= REAL_WORD_ZIPF)
    return round(real / len(candidates), 3)


def ocr_inside(page, box, page_height):
    """OCR'd lines that sit mostly inside one stored image, in reading order, with their average confidence."""
    lines = []
    for cell in page.parsed_page.textline_cells:
        if not cell.from_ocr:
            continue
        cell_box = cell.rect.to_bounding_box().to_top_left_origin(page_height)
        if cell_box.area() > 0 and box.intersection_area_with(cell_box) / cell_box.area() > 0.5:
            lines.append((cell_box.t, cell_box.l, cell.text, cell.confidence))
    lines.sort()
    text = "\n".join(line[2] for line in lines)
    confidence = round(sum(line[3] for line in lines) / len(lines), 3) if lines else None
    return text, confidence, len(lines)


def check_ocr(text, confidence, vocabulary):
    """Is the OCR result good enough to keep? Returns (ok, real-word share, reason)."""
    if not DO_OCR:
        return False, None, "OCR off"
    if not text.strip():
        return False, None, "no OCR text"
    share = real_word_share(text, vocabulary)
    if confidence is None or confidence < CONFIDENCE_MIN:
        return False, share, f"low confidence ({confidence})"
    if share is None or share < REAL_WORD_MIN:
        return False, share, f"few real words ({share})"
    return True, share, "passed"


def embedded_images(result, stem, vocabulary, page_words):
    """One record per image stored in the PDF, read from bitmap_resources
    (a fact from the file, not a guess by the layout model). With OCR on, each image's
    OCR text is checked; images that fail go to Gemini if USE_GEMINI is on."""
    doc = result.document
    records = []

    for page in result.pages:
        if page.parsed_page is None:
            continue
        page_no = page.page_no
        size = doc.pages[page_no].size
        page_area = size.width * size.height

        for i, bmp in enumerate(page.parsed_page.bitmap_resources):
            box = bmp.rect.to_bounding_box().to_top_left_origin(size.height)
            if box.area() <= 0:
                continue
            share = box.area() / page_area

            # Which layout-model pictures sit (mostly) inside this stored image?
            layout_pictures = []
            for pic in doc.pictures:
                for prov in pic.prov:
                    if prov.page_no != page_no:
                        continue
                    pic_box = prov.bbox.to_top_left_origin(size.height)
                    if pic_box.area() > 0 and box.intersection_area_with(pic_box) / pic_box.area() > 0.5:
                        layout_pictures.append(pic.self_ref)

            image = bmp.image.pil_image if bmp.image is not None else None
            saved_as = None
            if image is not None and SAVE_IMAGES:
                saved_as = f"{stem}_p{page_no}_stored_{i}.png"
                image.save(OUT_DIR / saved_as)

            # Read the text inside the image: OCR if it passes the checks, otherwise Gemini
            ocr_text, ocr_confidence, ocr_lines = ocr_inside(page, box, size.height)
            ocr_ok, word_share, check_reason = check_ocr(ocr_text, ocr_confidence, vocabulary)
            text, source, gemini_error = None, None, None
            if share < MIN_IMAGE_SHARE:
                check_reason = "image too small, not read"
            elif ocr_ok:
                text, source = ocr_text, "ocr"
            elif USE_GEMINI and image is not None:
                text, gemini_error = read_image_with_gemini(image, page_no)
                source = "gemini" if text else None

            if text and text.strip() == "NO_TEXT":
                status = "no text in image"
            elif text:
                status = f"read ({source})"
            else:
                status = "not read"

            text_file = None
            if text and status.startswith("read"):
                text_file = f"{stem}_p{page_no}_stored_{i}.txt"
                (OUT_DIR / text_file).write_text(text, encoding="utf-8")

            image_only = share > IMAGE_ONLY_SHARE and page_words[page_no] < IMAGE_ONLY_WORDS
            records.append({
                "page": page_no,
                "index": i,
                "bbox_tl": [round(v, 1) for v in (box.l, box.t, box.r, box.b)],
                "share_of_page": round(share, 3),
                "pixels": f"{image.width}x{image.height}" if image is not None else None,
                "image_sha256": hashlib.sha256(image.tobytes()).hexdigest() if image is not None else None,
                "type": "image_only" if image_only else "mixed",
                "page_text_layer_words": page_words[page_no],
                "layout_pictures": layout_pictures,     # [] = layout model found no picture here
                "saved_as": saved_as,
                "ocr_lines": ocr_lines,
                "ocr_confidence": ocr_confidence,
                "real_word_share": word_share,
                "ocr_check": check_reason,
                "source": source,                       # "ocr", "gemini" or None
                "gemini_error": gemini_error,
                "text_file": text_file,
                "status": status,
            })
    return records


def picture_labels(doc, images):
    """Label every layout-model picture: inside a stored image -> embedded, else not matched
    (no stored image found under it; it may be drawn, or span several stored images)."""
    embedded = {ref: img["status"] for img in images for ref in img["layout_pictures"]}
    labels = {}
    for pic in doc.pictures:
        pages = ", ".join(f"p{p}" for p in pages_of(pic)) or "p?"
        if pic.self_ref in embedded:
            labels[pic.self_ref] = f"embedded image ({pages}): stored in the PDF, text {embedded[pic.self_ref]}"
        else:
            labels[pic.self_ref] = f"picture not matched ({pages}): no stored image found under it"
    return labels


def labelled_markdown(doc, labels):
    """Body Markdown with each <!-- image --> replaced by its label.
    Returns (markdown, True) or, if the pictures can't be matched up, the plain
    placeholders and False."""
    md = doc.export_to_markdown(image_placeholder=PLACEHOLDER)
    order = [item.self_ref for item, _ in doc.iterate_items() if isinstance(item, PictureItem)]
    parts = md.split(PLACEHOLDER)
    if len(parts) - 1 != len(order):
        return md.replace(PLACEHOLDER, "<!-- image -->"), False
    out = parts[0]
    for ref, part in zip(order, parts[1:]):
        out += f"<!-- {labels.get(ref, 'picture')} -->" + part
    return out, True


_gemini_client = None


def gemini_key():
    """GEMINI_API_KEY from .env (or the environment). Accepts spaces, quotes and 'export'."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if "=" in line:
                name, value = line.split("=", 1)
                if name.strip() in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                    return value.strip().strip('"').strip("'")
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def read_image_with_gemini(image, page_no):
    """Send one stored image to Gemini. Returns (text, error); retries when Gemini is busy (503)."""
    global _gemini_client
    from google import genai
    from google.genai import types

    if _gemini_client is None:
        key = gemini_key()
        if not key:
            return None, "no GEMINI_API_KEY in .env"
        _gemini_client = genai.Client(api_key=key)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    contents = [types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png"), GEMINI_PROMPT]

    for attempt in range(1, 6):                          # up to 5 tries
        try:
            response = _gemini_client.models.generate_content(model=GEMINI_MODEL, contents=contents)
            return (response.text or "").strip() or None, None
        except Exception as e:
            if "503" in str(e) and attempt < 5:
                print(f"   p{page_no}: Gemini busy, retrying in {10 * attempt} s")
                time.sleep(10 * attempt)
            else:
                return None, f"{type(e).__name__}: {e}"



def docling_words(doc):
    """Words Docling stored: text, list markers and table cells.
    Returns (whole document, per page). An item that crosses a page break
    counts once in the total and on every page it touches."""
    total = Counter()
    by_page = {}

    def add(counter, item):
        total.update(counter)
        for p in pages_of(item):
            by_page.setdefault(p, Counter()).update(counter)

    for item in doc.texts:
        add(words(item.text) + words(getattr(item, "marker", "")), item)

    for table in doc.tables:
        cells = Counter()
        seen = set()
        for cell in table.data.table_cells:
            key = (cell.start_row_offset_idx, cell.start_col_offset_idx)
            if key not in seen:          # a merged cell is listed once per grid position
                seen.add(key)
                cells.update(words(cell.text))
        add(cells, table)

    return total, by_page


def text_check(doc, pdf_path):
    total, by_page = docling_words(doc)
    reference = Counter()
    flagged = []

    pdf = pdfium.PdfDocument(str(pdf_path))
    first, last = PAGE_RANGE if PAGE_RANGE else (1, len(pdf))
    first, last = max(1, first), min(last, len(pdf))   # same as Docling: ignore pages that don't exist
    for page_no in range(first, last + 1):
        textpage = pdf[page_no - 1].get_textpage()
        ref = words(textpage.get_text_range())
        reference.update(ref)
        n = sum(ref.values())
        missing = sum((ref - by_page.get(page_no, Counter())).values())
        if missing >= MIN_MISSING and missing / n > FLAG_MISSING:
            flagged.append({"page": page_no, "missing": missing, "of": n})
    pdf.close()

    n = sum(reference.values())
    found = sum((reference & total).values())
    return {
        "percent_read": round(100 * found / n, 2) if n else None,
        "reference": "pypdfium2 text layer",
        "reference_words": n,
        "missing_words": n - found,
        "extra_words": sum((total - reference).values()),
        "flagged_pages": flagged,
        "note": "text inside images is not in the text layer, so it is not counted here",
    }



def version_info(doc):
    found = Counter()
    for item, _ in doc.iterate_items(included_content_layers=ALL_LAYERS):
        for day, month, year in DATE_PATTERN.findall(getattr(item, "text", "") or ""):
            try:
                found[datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date().isoformat()] += 1
            except ValueError:
                pass                     # month name not recognised
    return {
        "updated_as_of": max(found) if found else None,     # latest date found
        "dates_found": dict(found.most_common()),
    }



def process_pdf(converter, pdf, manifest):
    file_hash = sha_256(pdf)
    known = manifest.get(file_hash)
    if known and (OUT_DIR / known["outputs"]["json"]).exists():
        # keep the saved results, so report.json still shows them for skipped files
        return {**known, "file": pdf.name, "status": "skipped", "parsed_status": known["status"],
                "reason": f"same file already parsed in this run as {known['file']}"}

    start = time.perf_counter()
    result = converter.convert(pdf, page_range=PAGE_RANGE) if PAGE_RANGE else converter.convert(pdf)
    seconds = round(time.perf_counter() - start, 1)
    doc = result.document
    stem = f"{pdf.stem}_{file_hash[:8]}"   # first 8 characters of the hash: each version of a file gets its own outputs

    doc.save_as_json(OUT_DIR / f"{stem}.json")
    vocabulary, page_words = pdf_text_layer(pdf)
    images = embedded_images(result, stem, vocabulary, page_words)
    labels = picture_labels(doc, images)
    md, labels_applied = labelled_markdown(doc, labels)
    (OUT_DIR / f"{stem}.md").write_text(md, encoding="utf-8") #d

    return {
        "file": pdf.name,
        "sha256": file_hash,
        "status": result.status.value,
        "errors": [str(e) for e in result.errors],
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
        "pages": len(doc.pages),
        "seconds": seconds,
        "version": version_info(doc),
        "text_check": text_check(doc, pdf),
        "pictures": {
            "stored_images": len(images),
            "layout_model_pictures": len(doc.pictures),
            "labelled_embedded": sum(1 for v in labels.values() if v.startswith("embedded")),
            "labelled_not_matched": sum(1 for v in labels.values() if v.startswith("picture not matched")),
            "markdown_labels_applied": labels_applied,
        },
        "embedded_images": images,
        "outputs": {"json": f"{stem}.json", "md": f"{stem}.md"},
    }


def print_summary(r):
    print(f"\n=== {r['file']} ===")
    if r["status"] in ("failed", "skipped"):
        print(f"{r['status'].upper()}: {r.get('error') or r.get('reason')}")
        return
    t, p = r["text_check"], r["pictures"]
    print(f"status {r['status']} | {r['pages']} pages | {r['seconds']} s | updated as of {r['version']['updated_as_of']}")
    print(f"text read: {t['percent_read']}% of {t['reference_words']} words "
          f"(missing {t['missing_words']}, extra {t['extra_words']}) - image text not included")
    if t["flagged_pages"]:
        print(f"   flagged pages: {[f['page'] for f in t['flagged_pages']]}")
    print(f"stored images: {p['stored_images']} | layout-model pictures: {p['layout_model_pictures']} "
          f"(embedded {p['labelled_embedded']}, not matched {p['labelled_not_matched']})")
    for img in r["embedded_images"]:
        print(f"   p{img['page']}: {img['type']:10} {img['pixels'] or 'no pixels':10} "
              f"{img['share_of_page']:.0%} of page, layout pictures inside: {len(img['layout_pictures'])}")
        print(f"      OCR confidence {img['ocr_confidence']}, real words {img['real_word_share']} "
              f"-> {img['ocr_check']} -> {img['status']}"
              + (f" ({img['gemini_error']})" if img["gemini_error"] else ""))



def package_versions():
    found = {}
    for pkg in ("docling", "docling-core", "docling-parse", "pypdfium2", "ocrmac", "wordfreq", "google-genai"):
        try:
            found[pkg] = version(pkg)
        except PackageNotFoundError:
            found[pkg] = None
    return found


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    converter = build_converter()

    records = []
    for pdf in sorted(RAW_DIR.glob("*.pdf")):
        try:
            record = process_pdf(converter, pdf, manifest)
        except Exception as e:           # one bad PDF must not stop the rest
            record = {"file": pdf.name, "status": "failed", "error": f"{type(e).__name__}: {e}"}
        if record["status"] == ConversionStatus.SUCCESS.value:
            manifest[record["sha256"]] = record   # only full successes are skipped next time
        records.append(record)
        print_summary(record)

    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = {
        "run_name": RUN_NAME,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "settings": {
            "backend": "docling-parse (default), include_bitmap_images=True",
            "do_ocr": DO_OCR,
            "ocr_engine": "ocrmac" if DO_OCR else None,
            "use_gemini": USE_GEMINI,
            "gemini_model": GEMINI_MODEL if USE_GEMINI else None,
            "ocr_checks": f"confidence >= {CONFIDENCE_MIN} and real-word share >= {REAL_WORD_MIN} "
                          f"(wordfreq zipf >= {REAL_WORD_ZIPF} or word in the PDF's own text)",
            "min_image_share": MIN_IMAGE_SHARE,
            "table_mode": TABLE_MODE.value,
            "page_range": PAGE_RANGE,
            "generate_parsed_pages": True,
            "save_images": SAVE_IMAGES,
            "image_only_rule": f"share > {IMAGE_ONLY_SHARE} and text-layer words < {IMAGE_ONLY_WORDS}",
            "text_check_reference": "pypdfium2 text layer",
        },
        "versions": package_versions(),
        "files": records,
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved to {OUT_DIR}")


if __name__ == "__main__":
    main()
