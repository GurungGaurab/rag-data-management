"""Docling stage: parse every PDF in data/raw and report what was read.

Run:     python pipeline.py
Output:  data/parsed/<RUN_NAME>/
           <file>.json                  full DoclingDocument (the source for chunking)
           <file>.md                    body text; each picture labelled embedded / drawn
           <file>_p<N>_stored_<i>.png   stored (embedded) images, if SAVE_IMAGES
           manifest.json                sha256 -> result; files already parsed are skipped
           report.json                  settings, versions and one record per PDF
"""

import hashlib
import html
import json
import re
import time
from collections import Counter
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium
from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode, TableStructureOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ContentLayer, PictureItem



RUN_NAME = "pipeline_v1"
DO_OCR = False                          # handbooks have a text layer
TABLE_MODE = TableFormerMode.ACCURATE
PAGE_RANGE = None                       # e.g. (1, 8) for a quick test; None = whole file
SAVE_IMAGES = True                      # write each stored image as a PNG into the run folder

IMAGE_ONLY_SHARE = 0.3                  # stored image covers more than 30% of the page...
IMAGE_ONLY_WORDS = 30                   # ...and the page has fewer than 30 body words -> image-only page
FLAG_MISSING = 0.05                     # flag a page if more than 5% of its text-layer words are missing...
MIN_MISSING = 3                         # ...and at least 3 words are missing (avoids noise on near-empty pages)

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

#Used to decided if the filed has already been paresed or not 
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
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=options,
            # default docling-parse backend; also decode the stored images' pixels
            backend_options=ThreadedDoclingParseBackendOptions(include_bitmap_images=True),
        )
    })


#Embedded images 
def body_words_per_page(doc):
    counts = Counter()
    for item in doc.texts:
        if item.content_layer == ContentLayer.BODY:
            n = sum(words(item.text).values())
            for p in pages_of(item):
                counts[p] += n
    return counts


def embedded_images(result, stem):
    """One record per image stored in the PDF, read from bitmap_resources
    (a fact from the file, not a guess by the layout model)."""
    doc = result.document
    body_words = body_words_per_page(doc)
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

            image_only = share > IMAGE_ONLY_SHARE and body_words[page_no] < IMAGE_ONLY_WORDS
            records.append({
                "page": page_no,
                "index": i,
                "bbox_tl": [round(v, 1) for v in (box.l, box.t, box.r, box.b)],
                "share_of_page": round(share, 3),
                "pixels": f"{image.width}x{image.height}" if image is not None else None,
                "image_sha256": hashlib.sha256(image.tobytes()).hexdigest() if image is not None else None,
                "type": "image_only" if image_only else "mixed",
                "page_body_words": body_words[page_no],
                "layout_pictures": layout_pictures,     # [] = layout model found no picture here
                "saved_as": saved_as,
                "status": "not read",                   # becomes "read" once Gemini is wired in
            })
    return records


def picture_labels(doc, images):
    """Label every layout-model picture: inside a stored image -> embedded, else drawn."""
    embedded = {ref for img in images for ref in img["layout_pictures"]}
    labels = {}
    for pic in doc.pictures:
        pages = ", ".join(f"p{p}" for p in pages_of(pic)) or "p?"
        if pic.self_ref in embedded:
            labels[pic.self_ref] = f"embedded image ({pages}): stored in the PDF, text inside not read"
        else:
            labels[pic.self_ref] = f"drawn picture ({pages}): not a stored image"
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


def read_image_with_gemini(image, page_no):
    """Later: send one stored image to Gemini and return its text."""
    pass



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
        return {"file": pdf.name, "sha256": file_hash, "status": "skipped",
                "reason": f"same file already parsed in this run as {known['file']}"}

    start = time.perf_counter()
    result = converter.convert(pdf, page_range=PAGE_RANGE) if PAGE_RANGE else converter.convert(pdf)
    seconds = round(time.perf_counter() - start, 1)
    doc = result.document
    stem = pdf.stem

    doc.save_as_json(OUT_DIR / f"{stem}.json")
    images = embedded_images(result, stem)
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
            "labelled_drawn": sum(1 for v in labels.values() if v.startswith("drawn")),
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
          f"(embedded {p['labelled_embedded']}, drawn {p['labelled_drawn']})")
    for img in r["embedded_images"]:
        print(f"   p{img['page']}: {img['type']:10} {img['pixels'] or 'no pixels':10} "
              f"{img['share_of_page']:.0%} of page, layout pictures inside: {len(img['layout_pictures'])}")



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
            "table_mode": TABLE_MODE.value,
            "page_range": PAGE_RANGE,
            "generate_parsed_pages": True,
            "save_images": SAVE_IMAGES,
            "image_only_rule": f"share > {IMAGE_ONLY_SHARE} and body words < {IMAGE_ONLY_WORDS}",
            "text_check_reference": "pypdfium2 text layer",
        },
        "versions": {pkg: version(pkg) for pkg in ("docling", "docling-core", "docling-parse", "pypdfium2")},
        "files": records,
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved to {OUT_DIR}")


if __name__ == "__main__":
    main()
