from pathlib import Path 

RUN_NAME = "rebuild_v1"
RECIPE_DIR = Path("data/raw")
OUT_DIR = Path("data/parsed") / RUN_NAME

print(OUT_DIR)
print(sorted(RECIPE_DIR.glob("*.pdf")))

