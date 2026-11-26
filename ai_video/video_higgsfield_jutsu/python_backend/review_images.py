"""Bound review vision inputs without dropping source samples or timestamps."""
import base64
from io import BytesIO
import math

from PIL import Image, ImageDraw, ImageOps


def pack_review_images(images, *, limit=4):
    """Analysis-only sheets: each labelled tile remains a separate sample."""
    if type(limit) is not int or limit < 1:
        raise ValueError('Image budget must be positive')
    encoded, references = [], []
    per_sheet = max(1, math.ceil(len(images) / limit))
    cell, header = 640, 32
    for start in range(0, len(images), per_sheet):
        entries = images[start:start + per_sheet]
        cols = min(2, len(entries))
        rows = math.ceil(len(entries) / cols)
        canvas = Image.new('RGB', (cols * cell, rows * (cell + header)), 'white')
        draw = ImageDraw.Draw(canvas)
        for offset, (label, path) in enumerate(entries):
            x, y = offset % cols * cell, offset // cols * (cell + header)
            index = start + offset + 1
            draw.text((x + 6, y + 8), f'SAMPLE {index}', fill='black')
            with Image.open(path) as original:
                image = ImageOps.exif_transpose(original).convert('RGB')
                source_size = list(image.size)
                image.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                px, py = x + (cell - image.width) // 2, y + header + (cell - image.height) // 2
                canvas.paste(image, (px, py))
            references.append({'sourceImageIndex': index, 'label': label, 'sheetIndex': len(encoded) + 1,
                'bbox': [px / canvas.width, py / canvas.height,
                         (px + image.width) / canvas.width, (py + image.height) / canvas.height],
                'sourceSize': source_size, 'tileSize': list(image.size)})
        output = BytesIO()
        canvas.save(output, 'JPEG', quality=90)
        encoded.append({'type': 'input_image', 'image_url': 'data:image/jpeg;base64,'
                        + base64.b64encode(output.getvalue()).decode('ascii')})
    return encoded, references
