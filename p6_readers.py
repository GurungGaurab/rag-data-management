"""Test: can Docling's built-in local readers see inside the p. 6 image?"""
import html
import re
import time
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
from docling.chunking import HybridChunker
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    OcrMacOptions,
    OcrMode,
    PdfPipelineOptions,
    PictureDescriptionVlmEngineOptions,
    RapidOcrOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

ROOT = Path(__file__).parent
PDFS = [ROOT / "data" / "raw" / "handbook_2025.pdf", ROOT / "data" / "raw" / "handbook_2026.pdf"]
PAGE = 6
OUT_FILE = ROOT / "data" / "parsed" / "p6_readers.txt"
PROMPT = "Copy out all the text in this image exactly, line by line. Do not summarise."
WORD = re.compile(r"\w+")


def words(text):
    return Counter(WORD.findall(html.unescape(text or "").lower()))


def smolvlm_options():
    opts = PictureDescriptionVlmEngineOptions.from_preset("smolvlm")
    opts.prompt = PROMPT
    opts.generation_config = {"max_new_tokens": 800, "do_sample": False}
    opts.picture_area_threshold = 0.0      # default 0.05 would skip small pictures
    return opts


TESTS = {
    "ocrmac": PdfPipelineOptions(do_ocr=True, ocr_options=OcrMacOptions()),
    "ocrmac_full_page": PdfPipelineOptions(do_ocr=True, ocr_options=OcrMacOptions(mode=OcrMode.FULL_PAGE)),
    
    "smolvlm": PdfPipelineOptions(
        do_ocr=False,
        do_picture_description=True,
        picture_description_options=smolvlm_options(),
        generate_picture_images=True,
        images_scale=2.0,
    ),
}


def text_layer_words(pdf_path):
    """Words in the PDF's own text layer on PAGE: everything that is NOT inside the image."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    textpage = pdf[PAGE - 1].get_textpage()
    found = words(textpage.get_text_range())
    pdf.close()
    return found


def all_doc_words(doc):
    """Every word Docling has for the page: text items (inside pictures too), table cells, descriptions."""
    found = Counter()
    for item in doc.texts:
        found.update(words(item.text))
    for table in doc.tables:
        for cell in table.data.table_cells:
            found.update(words(cell.text))
    for pic in doc.pictures:
        if pic.meta and pic.meta.description:
            found.update(words(pic.meta.description.text))
    return found


def chunk_words(doc):
    found = Counter()
    for chunk in HybridChunker().chunk(doc):
        found.update(words(chunk.text))
    return found


def descriptions(doc):
    return " ".join(p.meta.description.text for p in doc.pictures if p.meta and p.meta.description)


lines = []
for name, options in TESTS.items():
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    for pdf in PDFS:
        header = f"=== {name} | {pdf.name} p{PAGE} ==="
        try:
            reference = text_layer_words(pdf)
            start = time.perf_counter()
            doc = converter.convert(pdf, page_range=(PAGE, PAGE)).document
            seconds = round(time.perf_counter() - start, 1)

            recovered = all_doc_words(doc) - reference          # words that can only have come from the image
            in_chunks = (chunk_words(doc) - reference) & recovered
            summary = (f"{seconds} s | pictures: {len(doc.pictures)} | "
                       f"words from the image: {sum(recovered.values())} | "
                       f"of those in chunks: {sum(in_chunks.values())}")
            text = (f"WORDS FROM THE IMAGE (not in the PDF text layer):\n{' '.join(sorted(recovered.elements()))}\n\n"
                    f"DESCRIPTION:\n{descriptions(doc) or '(none)'}\n\n"
                    f"WHOLE PAGE AS DOCLING SEES IT (pictures opened up):\n"
                    f"{doc.export_to_markdown(traverse_pictures=True)}")
        except Exception as e:
            summary = f"FAILED: {type(e).__name__}: {e}"
            text = ""
        print(f"\n{header}\n{summary}")
        lines += [header, summary, text, ""]
        OUT_FILE.write_text("\n".join(lines), encoding="utf-8")   # saved after every test

print(f"\nFull text saved to {OUT_FILE}")
