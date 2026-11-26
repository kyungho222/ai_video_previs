"""Analysis-only contact sheets with reversible asset and crop references."""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps, PngImagePlugin

MAX_SHEET_SIDE = 3072
MAX_ASSET_SIDE = 1536
ASSETS_PER_SHEET = 4
SHEET_MARKER = 'previs_analysis_sheet_v2'


def analysis_image_max_side(path):
    """Only our bounded contact sheets bypass the ordinary analysis thumbnail."""
    with Image.open(path) as image:
        return MAX_SHEET_SIDE if image.info.get(SHEET_MARKER) == '1' else 768


def _sheet_groups(assets, combined, max_sheets):
    groups = [(category, [(i, a) for i, a in enumerate(assets, 1)
                         if category == 'all' or (a.get('role') == 'character') == (category == 'character')])
              for category in (('all',) if combined else ('character', 'other'))]
    groups = [(category, entries) for category, entries in groups if entries]
    if not groups:
        return []
    desired = [math.ceil(len(entries) / ASSETS_PER_SHEET) for _, entries in groups]
    budget = sum(desired) if max_sheets is None else max_sheets
    if type(budget) is not int or budget < 1:
        raise ValueError('At least one analysis image slot is required')
    if len(groups) > budget:
        groups = [('all', list(enumerate(assets, 1)))]
        desired = [math.ceil(len(assets) / ASSETS_PER_SHEET)]
    counts = [1] * len(groups)
    while sum(counts) < min(budget, sum(desired)):
        eligible = [i for i in range(len(groups)) if counts[i] < desired[i]]
        index = max(eligible, key=lambda i: len(groups[i][1]) / counts[i])
        counts[index] += 1
    batches = []
    for (category, entries), count in zip(groups, counts):
        # Balanced, deterministic batches retain the original asset indices.
        for index in range(count):
            start, end = index * len(entries) // count, (index + 1) * len(entries) // count
            batches.append((category, index + 1, entries[start:end]))
    return batches


def create_analysis_sheets(assets, directory, *, combined=False, max_sheets=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sheets, references = [], []
    for category, batch, entries in _sheet_groups(assets, combined, max_sheets):
        cols = len(entries) if len(entries) <= 3 else math.ceil(math.sqrt(len(entries)))
        rows = math.ceil(len(entries) / cols)
        native_sides = []
        for _, asset in entries:
            with Image.open(asset['path']) as source:
                native_sides.append(max(source.size))
        cell_w = cell_h = min(MAX_ASSET_SIDE, max(native_sides), MAX_SHEET_SIDE // max(cols, rows))
        header = min(40, cell_h // 8)
        prepared = []
        for index, asset in entries:
            with Image.open(asset['path']) as source:
                original_size = list(source.size)
                image = ImageOps.exif_transpose(source).convert('RGB')
                oriented_size = list(image.size)
                image.thumbnail((cell_w, cell_h-header), Image.Resampling.LANCZOS)
            prepared.append((index, asset, image, original_size, oriented_size))
        # Trim empty row padding without resizing any asset a second time.
        row_heights = [max(item[2].height for item in prepared[row*cols:(row+1)*cols]) + header
                       for row in range(rows)]
        canvas = Image.new('RGB', (cols * cell_w, sum(row_heights)), 'white')
        draw = ImageDraw.Draw(canvas)
        sheet_index = len(sheets) + 1
        for offset, (index, asset, image, original_size, oriented_size) in enumerate(prepared):
            row = offset // cols
            x, y = (offset % cols)*cell_w, sum(row_heights[:row])
            px, py = x + (cell_w-image.width)//2, y + header + (row_heights[row]-header-image.height)//2
            canvas.paste(image, (px, py))
            draw.text((x+6,y+6), 'ASSET ' + str(index), fill='black', font_size=24)
            references.append({'assetIndex':index, 'sheetIndex':sheet_index,
                'bbox':[px/canvas.width,py/canvas.height,(px+image.width)/canvas.width,(py+image.height)/canvas.height],
                'sourceId':asset.get('id') or str(asset.get('sourceIndex', index)),
                'sourceIndex':asset.get('sourceIndex',index), 'sourceCrop':asset.get('bbox'),
                'sourceSize':[asset.get('width', original_size[0]), asset.get('height', original_size[1])],
                'cropSize':original_size, 'role':asset.get('role'),
                'orientedSize':oriented_size, 'renderedSize':list(image.size),
                'sheetSize':list(canvas.size),
                'resizeScale':min(image.width/oriented_size[0], image.height/oriented_size[1])})
        target = directory / f'analysis-sheet-{category}-{batch}.png'
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text(SHEET_MARKER, '1')
        canvas.save(target, 'PNG', pnginfo=metadata)
        sheets.append(target)
    return sheets, sorted(references, key=lambda x:x['assetIndex'])
