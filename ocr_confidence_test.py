from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

ROOT = Path(__file__).parent
options = PdfPipelineOptions(do_ocr=True, ocr_options=OcrMacOptions(), generate_parsed_pages=True)
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

for pdf in sorted((ROOT / "data" / "raw").glob("*.pdf")):
    result = converter.convert(pdf, page_range=(1, 8))
    print(f"\n=== {pdf.name} ===")
    for page in result.pages:
        cells = [c for c in page.parsed_page.textline_cells if c.from_ocr] if page.parsed_page else []
        if not cells:
            continue
        score = result.confidence.pages[page.page_no].ocr_score
        lowest = sorted(cells, key=lambda c: c.confidence)[:3]
        print(f"p{page.page_no}: OCR score {score:.2f} from {len(cells)} OCR lines | lowest: "
              + "; ".join(f"{c.text!r} {c.confidence:.2f}" for c in lowest))