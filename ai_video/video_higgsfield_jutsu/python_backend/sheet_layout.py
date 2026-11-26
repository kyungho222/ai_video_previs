"""Find regular sheet divider candidates; visual semantics remain the model's job."""
from PIL import Image, ImageChops, ImageOps, ImageStat


def detect_sheet_layout(image):
    image = ImageOps.exif_transpose(image).convert('L')
    image.thumbnail((720, 720))
    width, height = image.size
    if min(width, height) < 60:
        return None
    candidates = []
    for orientation in ('rows', 'columns'):
        dim = height if orientation == 'rows' else width
        def line(pos):
            return image.crop((0, pos, width, pos + 1) if orientation == 'rows'
                              else (pos, 0, pos + 1, height))
        for count in range(2, 11):
            ratio = (width / (height / count) if orientation == 'rows'
                     else height / (width / count))
            if ratio < 1.5:
                continue
            centers, scores = [], []
            for k in range(1, count):
                target = round(dim * k / count)
                radius = max(2, round(dim * .012))
                scores_near = []
                for pos in range(max(4, target-radius), min(dim-4, target+radius+1)):
                    stripe = line(pos)
                    variance = ImageStat.Stat(stripe).var[0]
                    if variance > 450:
                        continue
                    contrast = (ImageStat.Stat(ImageChops.difference(stripe, line(pos-3))).mean[0]
                                + ImageStat.Stat(ImageChops.difference(stripe, line(pos+3))).mean[0]) / 2
                    if contrast >= 3:
                        scores_near.append((contrast / (1 + variance / 200), pos))
                if not scores_near:
                    break
                score, pos = max(scores_near)
                centers.append(pos / dim)
                scores.append(score)
            if len(centers) != count-1:
                continue
            boundaries = [0., *centers, 1.]
            regions = [[0., boundaries[i], 1., boundaries[i+1]] if orientation == 'rows'
                       else [boundaries[i], 0., boundaries[i+1], 1.] for i in range(count)]
            candidates.append((min(scores), count, orientation, regions))
    if not candidates:
        return None
    _, count, orientation, regions = max(candidates, key=lambda item: (item[0], item[1]))
    return {'orientation': orientation, 'count': count, 'regions': regions}
