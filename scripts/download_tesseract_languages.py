from pathlib import Path
import json
import requests

URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdoc/main/Data-Files-in-different-versions.md"

text = requests.get(URL, timeout=30).text

mapping = {}

for line in text.splitlines():
    if "|" not in line:
        continue

    cols = [c.strip() for c in line.split("|")]

    if len(cols) < 3:
        continue

    code = cols[1].strip("` ")

    name = cols[2].strip()

    if code and name:
        mapping[code] = name

out = Path("app/api/tools/ocr/tesseract_languages.json")

out.write_text(
    json.dumps(mapping, indent=2, ensure_ascii=False),
    encoding="utf8",
)

print(len(mapping), "languages")