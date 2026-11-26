"""Persist semantic identity links separately from measured per-frame geometry."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .asset_mapping_inventory import _sample_valid, attach_environment_references


def asset_key(asset):
    value = [asset.get('id') or asset.get('assetId'), asset.get('role'), asset.get('bbox')]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def binding_scope(source_hash, manifest):
    ids = sorted(o['id'] for o in (manifest or {}).get('objects', [])
                 if isinstance(o, dict) and isinstance(o.get('id'), str))
    return hashlib.sha256(json.dumps([source_hash, ids]).encode()).hexdigest()


class AssetObjectBindings:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS asset_object_bindings('
                       'scope TEXT,asset_key TEXT,data TEXT,PRIMARY KEY(scope,asset_key))')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, scope, assets):
        wanted = {asset_key(asset) for asset in assets}
        with self.db() as db:
            rows = db.execute('SELECT asset_key,data FROM asset_object_bindings WHERE scope=?', (scope,)).fetchall()
        return [json.loads(data) for key, data in rows if key in wanted]

    def save(self, scope, records):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for record in records:
                # Existing confirmed links survive AI responses; an explicit user link can replace them.
                sql = ('INSERT INTO asset_object_bindings VALUES(?,?,?) ON CONFLICT(scope,asset_key) '
                       + ('DO UPDATE SET data=excluded.data' if record.get('origin') == 'explicit' else 'DO NOTHING'))
                db.execute(sql, (scope, record['assetKey'], json.dumps(record, ensure_ascii=False)))


def _compatible(asset, source_ids, manifest):
    known = {o.get('id'): o for o in (manifest or {}).get('objects', []) if isinstance(o, dict)}
    if not isinstance(source_ids, list) or len(source_ids) != 1 or not isinstance(source_ids[0], str) or source_ids[0] not in known:
        return False
    obj = known[source_ids[0]]
    if obj.get('type') in ('CAMERA', 'LIGHT') or obj.get('role') == 'camera':
        return False
    kind = obj.get('kind')
    return not (kind and ((asset.get('role') == 'character' and kind == 'prop')
                         or (asset.get('role') == 'object' and kind in ('human','bird','quadruped'))))


def explicit_bindings(assets, manifest):
    records = []
    for asset in assets:
        source = asset.get('sourceObjectId')
        if source and _compatible(asset, [source], manifest):
            records.append({'assetKey':asset_key(asset), 'assetId':asset.get('id') or asset.get('assetId'),
                'role':asset.get('role'), 'entityId':'native_' + source,
                'sourceObjectIds':[source], 'label':asset.get('label') or asset.get('name') or source,
                'appearance':asset.get('appearance') or 'Use the registered asset image as the appearance authority.',
                'confidence':1.0, 'origin':'explicit'})
    return records


def capture_bindings(mapping, assets, manifest):
    """Only confirmed native links are reusable; no boxes, times, or visibility are stored."""
    records = []
    entities = mapping.get('entities', [])
    for index, asset in enumerate(assets, 1):
        if asset.get('role') in ('background','environment'):
            records.append({'assetKey':asset_key(asset), 'assetId':asset.get('id') or asset.get('assetId'),
                'role':asset['role'], 'entityId':'scene_environment', 'sourceObjectIds':[],
                'label':asset.get('label') or asset.get('name') or 'registered environment',
                'appearance':'Use the registered environment reference.', 'confidence':1., 'origin':'registered_environment'})
            continue
        matches = [e for e in entities if index in e.get('assetIndices', [])]
        if len(matches) != 1:
            continue
        entity = matches[0]
        confidence = entity.get('confidence')
        if (not _compatible(asset, entity.get('sourceObjectIds', []), manifest)
                or type(confidence) not in (float,int) or not math.isfinite(confidence) or not .65 <= confidence <= 1
                or any(not isinstance(entity.get(k), str) or not entity[k].strip() for k in ('id','label','appearance'))):
            continue
        records.append({'assetKey':asset_key(asset), 'assetId':asset.get('id') or asset.get('assetId'),
            'role':asset.get('role'), 'entityId':entity['id'],
            **{k:deepcopy(entity[k]) for k in ('sourceObjectIds','label','appearance','confidence')}, 'origin':'model'})
    return records


def frame_samples(source_ids, frames, manifest):
    if len(source_ids) != 1:
        return None
    samples = []
    for frame in frames:
        time = frame.get('time')
        matches = [f for f in (manifest or {}).get('frames', []) if type(time) in (float,int)
                   and type(f.get('time')) in (float,int) and abs(f['time']-time) < 1e-5]
        if len(matches) != 1:
            return None
        observations = [o for o in matches[0].get('subjects', []) if o.get('id') == source_ids[0]]
        if len(observations) != 1 or not _sample_valid(observations[0]):
            return None
        observation = observations[0]
        samples.append({'index':frame['index'], 'visible':observation['visible'],
                        'bbox':deepcopy(observation['bbox'])})
    return samples or None


def mapping_from_bindings(records, assets, frames, manifest):
    by_key = {r['assetKey']: r for r in records}
    result = {'cameraSummary':'Use the actual Blender layout camera for every frame.', 'entities':[]}
    report, by_object = [], {}
    for index, asset in enumerate(assets, 1):
        record = by_key.get(asset_key(asset))
        row = {'assetIndex':index, 'assetId':asset.get('id') or asset.get('assetId'),
               'linkStatus':'unresolved', 'spatialStatus':'missing_binding'}
        if asset.get('role') in ('background','environment'):
            row.update(linkStatus='linked', spatialStatus='environment_reference', source='registered_environment')
        elif record and _compatible(asset, record['sourceObjectIds'], manifest):
            row.update(linkStatus='linked', spatialStatus='missing_measurement',
                       sourceObjectIds=deepcopy(record['sourceObjectIds']), source=record['origin'])
            samples = frame_samples(record['sourceObjectIds'], frames, manifest)
            if samples:
                source = record['sourceObjectIds'][0]
                if source in by_object:
                    by_object[source]['assetIndices'].append(index)
                    if record['appearance'] not in by_object[source]['appearance']:
                        by_object[source]['appearance'] += '; ' + record['appearance']
                else:
                    entity = {k:deepcopy(record[k]) for k in ('label','appearance','sourceObjectIds','confidence')}
                    entity.update(id=record['entityId'], assetIndices=[index], frames=samples, frameSource='blender')
                    result['entities'].append(entity)
                    by_object[source] = entity
                row['spatialStatus'] = 'complete'
        report.append(row)
    return attach_environment_references(result, assets), report


def apply_confirmed_links(mapping, records, assets, frames, manifest):
    result = deepcopy(mapping)
    by_key = {r['assetKey']:r for r in records}
    changes = []
    for entity in result.get('entities', []):
        linked = [by_key.get(asset_key(assets[i-1])) for i in entity.get('assetIndices', [])
                  if type(i) is int and 1 <= i <= len(assets)]
        confirmed = {tuple(r['sourceObjectIds']) for r in linked if r}
        if len(confirmed) > 1:
            raise ValueError('하나의 매핑에 서로 다른 확정 객체 연결이 포함되었습니다: ' + str(entity.get('id')))
        if len(confirmed) == 1:
            ids = list(next(iter(confirmed)))
            if entity.get('sourceObjectIds') != ids:
                entity['sourceObjectIds'] = ids
                changes.append({'entityId':entity.get('id'), 'reason':'confirmed_asset_object_binding'})
        samples = frame_samples(entity.get('sourceObjectIds', []),frames,manifest)
        if samples:
            entity['frames'] = samples
            entity['frameSource'] = 'blender'
            changes.append({'entityId':entity.get('id'), 'reason':'actual_blender_frame_measurements'})
    return result, changes
