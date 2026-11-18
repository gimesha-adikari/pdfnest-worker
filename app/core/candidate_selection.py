"""Read-only, conservative whole-word semantics from the *candidate PDF*.

This does not modify PDF bytes or infer text from an editing request. Hidden
text is exposed only when an exact independently shaped raster-word image is
present once at the same baseline. Arbitrary OCR remains nonselectable.
"""
from hashlib import sha256
from collections import Counter
from io import BytesIO
from pathlib import Path
import math
import re
from typing import Callable

import pymupdf as fitz
from PIL import Image, ImageChops

from app.core.scanned_word_compiler import _SCRIPT_FONTS, _NATIVE_FONT_ALIASES, _render_shaped_script_patch, _script_profile

SELECTION_VERSION = 'platen_candidate_words.v1'
MAX_SELECTION_WORDS = 1000
MAX_SELECTION_PAGES = 100


def _text(trace):
    return ''.join(chr(char[0]) for char in trace.get('chars', ()))


def _intersects(left, right):
    area = fitz.Rect(left) & fitz.Rect(right)
    return not area.is_empty and area.width > .01 and area.height > .01


def _unobscured(page, rect, traces, own_trace, own_image=None, opaque_patch=False):
    if any(_intersects(rect, a.rect) for a in list(page.annots() or ()) + list(page.widgets() or ())):
        return False
    if any(_intersects(rect, d['rect']) for d in page.get_drawings()):
        return False
    if any(t is not own_trace and any(not chr(c[0]).isspace() and _intersects(rect,c[3]) for c in t.get('chars',())) for t in traces):
        return False
    images = page.get_image_info(xrefs=True)
    own_number = next((i['number'] for i in images if i.get('xref') == own_image), -1)
    return not any(i.get('xref') != own_image and _intersects(rect, i['bbox'])
                   and not (opaque_patch and i['number'] < own_number) for i in images)


def _script_word(page, doc, trace, traces, image_occurrences):
    text = _text(trace)
    if not text or len(text) > 256 or any(c.isspace() for c in text):
        return None
    script, reason = _script_profile(text)
    if reason or script not in _SCRIPT_FONTS:
        return None
    if trace.get('type') != 3 or trace.get('opacity') != 1 or trace.get('wmode') != 0 or tuple(trace.get('dir', ())) != (1., 0.):
        return None
    profile = _SCRIPT_FONTS[script]
    # A name is not font identity. Require the candidate's embedded bytes.
    known_font = Path(profile['fontfile']).read_bytes()
    fonts = [f for f in page.get_fonts(full=True) if re.sub(r'[^a-z]', '', f[3].lower()) == re.sub(r'[^a-z]', '', trace['font'].lower())]
    if len(fonts) != 1 or doc.extract_font(fonts[0][0])[3] != known_font:
        return None
    color = tuple(trace.get('color', ()))
    if color != (0., 0., 0.):
        return None
    shaped, error = _render_shaped_script_patch(text, profile, trace['size'], 300., color)
    if error:
        return None
    oracle = Image.open(BytesIO(shaped['png'])).convert('RGBA')
    origins = [c[2] for c in trace['chars']]
    if not origins or any(abs(o[1] - origins[0][1]) > .01 for o in origins):
        return None
    expected = fitz.Rect(origins[0][0]+shaped['offset_x'], origins[0][1]+shaped['offset_y'],
                         origins[0][0]+shaped['offset_x']+shaped['width'], origins[0][1]+shaped['offset_y']+shaped['height'])
    matches = []
    for image in page.get_images(full=True):
        xref, mask = image[:2]
        if image_occurrences[xref] != 1 or image[2]*image[3] > 1_000_000:
            continue
        occurrences = page.get_image_rects(xref, transform=True)
        # Shared XObject or multiple occurrences are never made selectable.
        if len(occurrences) != 1:
            continue
        rect, matrix = occurrences[0]
        if abs(matrix.b) > 1e-6 or abs(matrix.c) > 1e-6 or matrix.a <= 0 or matrix.d <= 0:
            continue
        if mask:
            if any(abs(a-b) > .001 for a,b in zip(rect, expected)):
                continue
            pix = fitz.Pixmap(fitz.Pixmap(doc, xref), fitz.Pixmap(doc, mask))
            if pix.n != 4 or (pix.width,pix.height) != oracle.size or pix.samples != oracle.tobytes():
                continue
            word_rect = rect
        else:
            # Accept a clean searchable-scan patch only when *every* RGB byte
            # equals independently shaped candidate text composited on white.
            # Locate ink from the actual image; never stretch hidden text boxes.
            pix = fitz.Pixmap(doc,xref)
            if pix.n != 3:
                continue
            actual = Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            ink = ImageChops.difference(actual,Image.new('RGB',actual.size,'white')).getbbox()
            white_word = Image.new('RGBA',oracle.size,'white'); white_word.alpha_composite(oracle)
            oracle_ink = ImageChops.difference(white_word.convert('RGB'),Image.new('RGB',oracle.size,'white')).getbbox()
            if not ink or not oracle_ink:
                continue
            x,y = ink[0]-oracle_ink[0],ink[1]-oracle_ink[1]
            if x<0 or y<0 or x+oracle.width>pix.width or y+oracle.height>pix.height:
                continue
            comparison = Image.new('RGBA',actual.size,'white'); comparison.alpha_composite(oracle,(x,y))
            if comparison.convert('RGB').tobytes() != actual.tobytes():
                continue
            word_rect = fitz.Rect(rect.x0+x*rect.width/pix.width, rect.y0+y*rect.height/pix.height,
                                 rect.x0+(x+oracle.width)*rect.width/pix.width, rect.y0+(y+oracle.height)*rect.height/pix.height)
            if not _intersects(word_rect,trace['bbox']):
                continue
        if not _unobscured(page, word_rect, traces, trace, xref, opaque_patch=not mask):
            continue
        matches.append(list(word_rect))
    if len(matches) != 1:
        return None
    return {'text': text, 'rect': matches[0], 'representation': 'verified_raster_word', 'selection': 'whole_word'}


def _native_words(page, trace, traces):
    text = _text(trace)
    if trace.get('type') != 0 or trace.get('opacity') != 1 or trace.get('wmode') != 0 or tuple(trace.get('dir', ())) != (1., 0.):
        return []
    # Partial glyph/cluster selection is deliberately outside this first subset.
    if not text or not all(0x20 <= ord(c) <= 0x7e for c in text):
        return []
    if trace.get('font') not in _NATIVE_FONT_ALIASES:
        return []
    fonts = [f for f in page.get_fonts(full=True) if f[3] == trace['font']]
    if len(fonts) != 1 or fonts[0][1:3] != ('n/a', 'Type1') or fonts[0][5] != 'WinAnsiEncoding':
        return []
    chars = trace['chars']
    font = fitz.Font(_NATIVE_FONT_ALIASES[trace['font']])
    if len(chars) != len(text) or any(c[1] != font.has_glyph(c[0]) for c in chars):
        return []
    result=[]
    for match in re.finditer(r'\S+', text):
        selected=chars[match.start():match.end()]
        rect=fitz.Rect(selected[0][3])
        for char in selected[1:]: rect |= fitz.Rect(char[3])
        if rect.is_empty or not _unobscured(page,rect,traces,trace):
            continue
        result.append({'text':match.group(),'rect':list(rect),'representation':'native_visible_word','selection':'whole_word'})
    return result


def inspect_candidate_selection(input_path: str, artifact_sha256: str, cancellation_check: Callable[[], None] | None = None):
    data=Path(input_path).read_bytes()
    if sha256(data).hexdigest() != artifact_sha256:
        raise ValueError('candidate identity mismatch')
    result={'version':SELECTION_VERSION,'artifact_sha256':artifact_sha256,'pages':[]}
    with fitz.open(stream=data,filetype='pdf') as doc:
        if doc.needs_pass or len(doc)>MAX_SELECTION_PAGES:
            return result
        image_occurrences = Counter(i['xref'] for p in doc for i in p.get_image_info(xrefs=True))
        total=0
        for page in doc:
            if cancellation_check: cancellation_check()
            record={'page_num':page.number+1,'width':page.rect.width,'height':page.rect.height,'words':[]}
            result['pages'].append(record)
            # No new geometry is qualified through this semantic path.
            if page.rotation != 0 or page.cropbox_position != fitz.Point(0,0):
                continue
            if page.get_xobjects() or any(re.search(rb'(?<!\S)W\*?(?!\S)', doc.xref_stream(x)) for x in page.get_contents()):
                continue
            traces=page.get_texttrace()
            for trace in traces:
                if cancellation_check: cancellation_check()
                try:
                    hidden = _script_word(page,doc,trace,traces,image_occurrences) if trace.get('type')==3 else None
                    words=[hidden] if hidden else _native_words(page,trace,traces)
                except (ValueError,RuntimeError,OSError,KeyError,TypeError):
                    words=[]
                for word in words:
                    rect=fitz.Rect(word['rect'])
                    if not all(math.isfinite(x) for x in rect) or not page.rect.contains(rect):continue
                    if any(_intersects(rect,w['rect']) for w in record['words']):continue
                    record['words'].append(word);total+=1
                    if total>MAX_SELECTION_WORDS:
                        return {'version':SELECTION_VERSION,'artifact_sha256':artifact_sha256,'pages':[]}
    return result
