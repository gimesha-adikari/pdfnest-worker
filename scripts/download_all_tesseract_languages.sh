#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

TESSDATA_DIR="${TESSDATA_DIR:-$PROJECT_ROOT/tesseract/tessdata}"

mkdir -p "$TESSDATA_DIR"

# Try to locate the system tessdata directory.
SYSTEM_TESSDATA=""

for dir in \
    "/usr/share/tesseract-ocr/5/tessdata" \
    "/usr/share/tesseract-ocr/4.00/tessdata" \
    "/usr/share/tesseract-ocr/tessdata" \
    "/usr/share/tessdata" \
    "/usr/local/share/tessdata"
do
    if [[ -d "$dir" ]]; then
        SYSTEM_TESSDATA="$dir"
        break
    fi
done

if [[ -z "$SYSTEM_TESSDATA" ]]; then
    echo "WARNING: Could not locate system tessdata directory."
    echo "Language files will be downloaded, but configs/pdf may be unavailable."
else
    echo "Using system tessdata:"
    echo "  $SYSTEM_TESSDATA"
fi

echo
echo "Downloading language list..."

curl -fsSL \
    "https://api.github.com/repos/tesseract-ocr/tessdata_fast/contents" |
jq -r '.[] | select(.name | endswith(".traineddata")) | .name' |
while read -r file; do
    if [[ -f "$TESSDATA_DIR/$file" ]]; then
        echo "✓ $file already exists"
        continue
    fi

    echo "Downloading $file..."

    curl -fsSL \
        "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/$file" \
        -o "$TESSDATA_DIR/$file"
done

if [[ -n "$SYSTEM_TESSDATA" ]]; then
    echo
    echo "Copying Tesseract support files..."

    if [[ -f "$SYSTEM_TESSDATA/pdf.ttf" ]]; then
        cp -f "$SYSTEM_TESSDATA/pdf.ttf" "$TESSDATA_DIR/"
        echo "✓ pdf.ttf"
    fi

    if [[ -d "$SYSTEM_TESSDATA/configs" ]]; then
        rm -rf "$TESSDATA_DIR/configs"
        cp -a "$SYSTEM_TESSDATA/configs" "$TESSDATA_DIR/"
        echo "✓ configs/"
    fi

    if [[ -d "$SYSTEM_TESSDATA/tessconfigs" ]]; then
        rm -rf "$TESSDATA_DIR/tessconfigs"
        cp -a "$SYSTEM_TESSDATA/tessconfigs" "$TESSDATA_DIR/"
        echo "✓ tessconfigs/"
    fi
fi

echo
echo "Verifying installation..."

[[ -f "$TESSDATA_DIR/configs/pdf" ]] \
    && echo "✓ PDF config found" \
    || echo "✗ Missing configs/pdf"

[[ -f "$TESSDATA_DIR/pdf.ttf" ]] \
    && echo "✓ pdf.ttf found" \
    || echo "✗ Missing pdf.ttf"

echo
echo "Installed languages:"

find "$TESSDATA_DIR" -name '*.traineddata' \
| sed 's#.*/##' \
| sed 's/\.traineddata$//' \
| sort

echo
echo "Total: $(find "$TESSDATA_DIR" -name '*.traineddata' | wc -l) languages"

echo
echo "Tessdata directory:"
echo "  $TESSDATA_DIR"