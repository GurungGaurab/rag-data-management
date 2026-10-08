from pathlib import Path 
from docling.document_converter import DocumentConverter
from docling_core.types.doc import ContentLayer

menu_pdf = Path(__file__).parent /"data" /"raw"/ "handbook_2026.pdf"

converter = DocumentConverter()
result = converter.convert(menu_pdf, page_range=(24,26))
doc = result.document

print(doc.export_to_markdown(included_content_layers={
    ContentLayer.BODY, ContentLayer.FURNITURE
}))
