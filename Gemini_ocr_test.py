"""Test: can Gemini read the text inside the stored images?"""
import os
import time
from pathlib import Path

from google import genai
from google.genai import types

ROOT = Path(__file__).parent
IMAGES_DIR = ROOT / "data" / "parsed" / "pipeline_v1"
OUT_FILE = ROOT / "data" / "parsed" / "gemini_test.txt"

MODEL = "gemini-3.5-flash-lite"     # copy a free Flash model name from AI Studio
LIST_MODELS = False          # True = just print the available model names and stop

IMAGES = [
    "handbook_2026_p6_stored_0.png",   # text-based image (Graduate Attributes)
    "handbook_2026_p7_stored_0.png",   # dense, table-like image (calendar)
]

PROMPT = (
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


def load_key():
    """Read GEMINI_API_KEY from .env (or from the environment)."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("GEMINI_API_KEY")


client = genai.Client(api_key=load_key())

if LIST_MODELS:
    for m in client.models.list():
        print(m.name)
    raise SystemExit

OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
lines = []
for name in IMAGES:
    path = IMAGES_DIR / name
    header = f"=== {MODEL} | {name} ==="
    try:
        start = time.perf_counter()
        response = client.models.generate_content(
            model=MODEL,
            contents=[types.Part.from_bytes(data=path.read_bytes(), mime_type="image/png"), PROMPT],
        )
        seconds = round(time.perf_counter() - start, 1)
        text = response.text or "(no text returned)"
        summary = f"{seconds} s | {len(text.split())} words"
    except Exception as e:
        summary = f"FAILED: {type(e).__name__}: {e}"
        text = ""
    print(f"\n{header}\n{summary}\n{text[:1500]}")
    lines += [header, summary, text, ""]
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")   # saved after every image

print(f"\nFull output saved to {OUT_FILE}")