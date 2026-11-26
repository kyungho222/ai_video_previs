"""Analysis-only contact sheets with reversible asset and crop references."""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps


def create_analysis_sheets(assets, directory, *, combined=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sheets, references = [], []
    for category in (('all',) if combined else ('character', 'other')):
        entries = [(i, a) for i, a in enumerate(assets, 1)
                   if category == 'all' or (a.get('role') == 'character') == (category == 'character')]
        if not entries:
            continue
        cols = min(3, len(entries)); rows = math.ceil(len(entries) / cols)
        cell_w, cell_h, header = 512, 512, 28
        canvas = Image.new('RGB', (cols * cell_w, rows * (cell_h + header)), 'white')
        draw = ImageDraw.Draw(canvas)
        sheet_index = len(sheets) + 1
        for offset, (index, asset) in enumerate(entries):
            x, y = (offset % cols)*cell_w, (offset // cols)*(cell_h+header)
            with Image.open(asset['path']) as source:
                original_size = list(source.size)
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
                px, py = x + (cell_w-image.width)//2, y + header + (cell_h-image.height)//2
                canvas.paste(image, (px, py))
            draw.text((x+6,y+6), 'ASSET ' + str(index), fill='black')
            references.append({'assetIndex':index, 'sheetIndex':sheet_index,
                'bbox':[px/canvas.width,py/canvas.height,(px+image.width)/canvas.width,(py+image.height)/canvas.height],
                'sourceId':asset.get('id') or str(asset.get('sourceIndex', index)),
                'sourceIndex':asset.get('sourceIndex',index), 'sourceCrop':asset.get('bbox'),
                'sourceSize':[asset.get('width', original_size[0]), asset.get('height', original_size[1])],
                'cropSize':original_size, 'role':asset.get('role')})
        target = directory / ('analysis-sheet-' + category + '.png')
        canvas.save(target, 'PNG')
        sheets.append(target)
    return sheets, sorted(references, key=lambda x:x['assetIndex'])
