"""Small, bounded blocking models, expressed in each entity's local unit box."""
import re


def _p(shape, pos, size, shade=1):
    return (shape, pos, size, shade)


def _box(pos, size, shade=1):
    return _p('box', pos, size, shade)


def _sphere(pos, size, shade=1):
    return _p('sphere', pos, size, shade)


def _cyl(pos, size, shade=1):
    return _p('cylinder', pos, size, shade)


def _legs(z=.35):
    return [_box((x, y, z / 2), (.09, .09, z), .75) for x in (-.4, .4) for y in (-.35, .35)]


def _wheels():
    return [_sphere((x, y, .20), (.22, .22, .28), .35) for x in (-.38, .38) for y in (-.36, .36)]


CATALOG = {
    'blackboard': (('칠판','blackboard','whiteboard','chalkboard'), [
        _box((0,0,.5),(1,.12,1)), _box((0,.08,.5),(.94,.04,.9),.8),
        _box((0,.13,.04),(1,.22,.06),1.2)]),
    'table': (('테이블','식탁','책상','table','desk'), [_box((0,0,.78),(1,1,.12))]+_legs(.72)),
    'chair': (('의자','chair'), [_box((0,0,.48),(.9,.85,.12)), _box((0,-.38,.74),(.9,.1,.52))]+_legs(.42)),
    'sofa': (('소파','쇼파','sofa','couch'), [_box((0,0,.31),(1,.9,.5)),_box((0,-.4,.72),(1,.18,.55),.85),_box((-.45,0,.55),(.1,.9,.4),.85),_box((.45,0,.55),(.1,.9,.4),.85)]),
    'bed': (('침대','bed'), [_box((0,0,.3),(1,1,.35)),_box((0,-.43,.65),(1,.14,.65),.8),_box((0,-.24,.52),(.6,.25,.12),1.15)]),
    'shelf': (('선반','책장','shelf','shelves','bookcase'), [_box((x,0,.5),(.08,.7,1)) for x in (-.46,.46)]+[_box((0,0,z),(1,.7,.07),.8) for z in (.08,.38,.68,.95)]),
    'door': (('문','door'), [_box((0,0,.5),(1,.15,1)),_sphere((.36,.1,.48),(.08,.08,.08),1.4)]),
    'window': (('창문','window'), [_box((x,0,.5),(.08,.16,1)) for x in (-.46,.46)]+[_box((0,0,z),(1,.16,.08)) for z in (.04,.5,.96)]+[_box((0,0,.5),(.06,.1,1))]),
    'stairs': (('계단','stairs','staircase','steps'), [_box((0,-.4+i*.2,(i+1)*.1),(1,.2,(i+1)*.2)) for i in range(5)]),
    'wall': (('벽','wall'), [_box((0,0,.5),(1,.22,1)),_box((0,-.12,.08),(1,.04,.12),.8)]),
    'tree': (('나무','tree'), [_cyl((0,0,.35),(.18,.18,.7),.55),_sphere((0,0,.78),(.9,.9,.45),1.1),_sphere((-.2,.05,.72),(.55,.6,.36))]),
    'flower': (('꽃','flower'), [_cyl((0,0,.35),(.07,.07,.7),.65),_sphere((0,0,.8),(.22,.22,.18),1.3)]+[_sphere((x,y,.8),(.28,.28,.15)) for x,y in ((-.22,0),(.22,0),(0,-.22),(0,.22))]),
    'rock': (('바위','돌','rock','boulder','stone'), [_sphere((0,0,.38),(1,.8,.7)),_sphere((.25,.1,.5),(.45,.45,.5),.85)]),
    'pot': (('화분','flowerpot','plant pot','plantpot'), [_cyl((0,0,.22),(.65,.65,.44),.75),_cyl((0,0,.62),(.08,.08,.45),.7),_sphere((0,0,.8),(.8,.8,.4),1.1)]),
    'cup': (('컵','머그','cup','mug'), [_cyl((0,0,.4),(.72,.72,.8)),_box((.43,0,.52),(.14,.18,.35),.8),_box((.34,0,.67),(.23,.18,.08),.8)]),
    'bottle': (('병','bottle'), [_cyl((0,0,.38),(.65,.65,.76)),_cyl((0,0,.82),(.25,.25,.2),.85),_cyl((0,0,.96),(.29,.29,.08),.65)]),
    'book': (('책','book'), [_box((0,0,.5),(1,.75,.82)),_box((0,0,.91),(1,.8,.08),.7),_box((-.47,0,.5),(.07,.8,.9),.7)]),
    'box': (('상자','박스','box','package'), [_box((0,0,.45),(1,1,.9)),_box((0,0,.93),(1.02,1.02,.1),.8)]),
    'bag': (('가방','bag','handbag'), [_box((0,0,.4),(.9,.4,.8)),_box((-.25,0,.88),(.08,.12,.25),.7),_box((.25,0,.88),(.08,.12,.25),.7),_box((0,0,.99),(.55,.12,.08),.7)]),
    'phone': (('휴대전화','휴대폰','스마트폰','핸드폰','phone','smartphone'), [_box((0,0,.5),(.65,.12,1)),_box((0,-.07,.53),(.53,.02,.75),.35)]),
    'monitor': (('모니터','monitor'), [_box((0,0,.65),(1,.12,.65)),_box((0,0,.25),(.1,.12,.3),.7),_box((0,0,.07),(.5,.4,.1),.7)]),
    'display': (('스크린','화면판','전광판','그래프','screen','display panel','graph','chart'), [
        _box((0,0,.5),(1,.12,1),.55), _box((0,.075,.5),(.93,.03,.90),1.1)]),
    'laptop': (('노트북','laptop'), [_box((0,0,.15),(1,.9,.12)),_box((0,-.4,.55),(1,.1,.8)),_box((0,-.34,.58),(.85,.02,.65),.35)]),
    'camera': (('카메라','camera'), [_box((0,0,.5),(1,.65,.6)),_cyl((0,-.4,.5),(.4,.4,.3),.45),_box((.25,0,.86),(.25,.3,.12),.75)]),
    'plate': (('접시','plate'), [_cyl((0,0,.1),(1,1,.18)),_cyl((0,0,.19),(.7,.7,.04),.85)]),
    'bowl': (('그릇','bowl'), [_cyl((0,0,.32),(.85,.85,.55)),_cyl((0,0,.62),(1,1,.1),.8)]),
    'food': (('음식','food','meal'), [_box((0,0,.08),(1,1,.12),.8),_sphere((0,0,.35),(.7,.7,.55),1.15)]),
    'drink': (('음료','drink','beverage'), [_cyl((0,0,.4),(.65,.65,.8)),_cyl((0,0,.82),(.7,.7,.08),.8),_cyl((.15,0,.92),(.05,.05,.16),1.3)]),
    'bench': (('벤치','bench'), [_box((0,0,.48),(1,.55,.1)),_box((0,-.24,.75),(1,.1,.45))]+_legs(.43)),
    'streetlight': (('가로등','streetlight','street lamp'), [_cyl((0,0,.47),(.1,.1,.94),.7),_box((.18,0,.94),(.4,.1,.08)),_sphere((.35,0,.9),(.2,.2,.12),1.4)]),
    'sign': (('표지판','signpost','sign'), [_cyl((0,0,.4),(.1,.1,.8),.7),_box((0,0,.8),(.9,.12,.35))]),
    'fence': (('울타리','fence'), [_box((x,0,.5),(.08,.1,1)) for x in (-.45,-.2,.05,.3,.45)]+[_box((0,0,z),(1,.08,.08),.8) for z in (.25,.7)]),
    'car': (('자동차','차량','car','automobile'), [_box((0,0,.36),(1,.75,.38)),_box((0,-.05,.66),(.62,.7,.3),.8)]+_wheels()),
    'bicycle': (('자전거','bicycle','bike'), [_sphere((0,y,.32),(.12,.55,.55),.4) for y in (-.36,.36)]+[_box((0,0,.47),(.1,.65,.12)),_box((0,-.2,.72),(.1,.1,.45)),_box((0,.25,.72),(.1,.1,.42))]),
    'bus': (('버스','bus'), [_box((0,0,.53),(1,.8,.7)),_box((0,-.41,.7),(.8,.03,.25),.4)]+_wheels()),
    'microphone': (('마이크','microphone','mic'), [_cyl((0,0,.4),(.18,.18,.8),.75),_sphere((0,0,.87),(.4,.4,.26),1.1)]),
    'stage': (('무대','stage','platform'), [_box((0,0,.18),(1,1,.36)),_box((0,0,.39),(1,1,.06),1.1)]),
    'doll': (('인형','doll','teddy'), [_sphere((0,0,.44),(.5,.35,.5)),_sphere((0,0,.8),(.45,.4,.4),1.1)]+[_sphere((x,0,.35),(.2,.25,.45)) for x in (-.3,.3)]),
    'ball': (('공','ball'), [_sphere((0,0,.5),(1,1,1)),_sphere((0,-.46,.5),(.2,.08,.2),.7)]),
}


def scene_prop_parts(label):
    text = str(label or '').casefold()
    matches = []
    for aliases, parts in CATALOG.values():
        for alias in aliases:
            if alias.isascii():
                pattern = r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])'
            elif len(alias) == 1:
                pattern = r'(?<![가-힣])' + re.escape(alias) + r'(?![가-힣])'
            else:
                pattern = re.escape(alias)
            match = re.search(pattern, text)
            if match:
                matches.append((match.end(), len(alias), parts))
    return max(matches, key=lambda item: (item[0], item[1]))[2] if matches else None


def is_display_prop(label):
    parts = scene_prop_parts(label)
    return parts is not None and any(parts == CATALOG[key][1] for key in ('display', 'monitor', 'blackboard'))
