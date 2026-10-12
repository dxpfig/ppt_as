"""PPTD animations as native PresentationML timing behavior.

Format references (not evidence of local PowerPoint playback):
* ISO/IEC 29500-1 19.5, reflected by Microsoft's Open XML SDK schemas:
  https://github.com/dotnet/Open-XML-SDK/blob/main/data/schemas/schemas_openxmlformats_org_presentationml_2006_main.json
* https://learn.microsoft.com/en-us/office/open-xml/presentation/working-with-animation
* MS-OE376 4.6.2: animClr requires an explicit starting color in Office:
  https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oe376/625cf947-1313-4aef-ae37-a2f746b379b3
* LibreOffice's PPTXAnimationExport confirms repeatCount is in thousandths:
  https://github.com/LibreOffice/core/blob/master/sd/source/filter/eppt/pptx-animations.cxx
* Native transparency pairs set(style.opacity) with animEffect(image, prLst):
  https://github.com/BramAlkema/pptx-animation-skill/blob/main/oracle/emph/behaviors/transparency.xml

The adapter creates only ordinary p:timing XML, not private extension fields.
Audit records deliberately say structure rather than verified PowerPoint playback.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from lxml import etree
from pptx.oxml.xmlchemy import OxmlElement

DURATIONS = {
    'appear': 0, 'fade-in': 500, 'fly-in': 500, 'zoom-in': 500,
    'wipe-in': 500, 'float-in': 500, 'peek-in': 500, 'rise-in': 1000,
    'pulse': 600, 'grow-shrink': 2000, 'spin': 2000, 'teeter': 1000,
    'fill-color': 2000, 'transparency': 2000, 'color-pulse': 2000,
    'disappear': 0, 'fade-out': 500, 'fly-out': 500, 'zoom-out': 500,
    'wipe-out': 500, 'float-out': 500, 'motion-path': 2000,
}
ENTRANCE = {'appear', 'fade-in', 'fly-in', 'zoom-in', 'wipe-in', 'float-in', 'peek-in', 'rise-in'}
EXIT = {'disappear', 'fade-out', 'fly-out', 'zoom-out', 'wipe-out', 'float-out'}
TOKEN = re.compile(r'[MLQCZmlqcz]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?')


def _number(value):
    return format(float(value), '.12g')


def _node(parent, name, **attrs):
    element = OxmlElement(name)
    for key, value in attrs.items():
        if value is not None:
            element.set(key, str(value))
    if parent is not None:
        parent.append(element)
    return element


def _path(path, width, height, fail):
    """Validate SVG's restricted single subpath; convert Q to C and px to slide units."""
    if not isinstance(path, str):
        fail('path is required')
    tokens, last = [], 0
    for match in TOKEN.finditer(path):
        if path[last:match.start()].strip(' \t\r\n,'):
            fail('path only supports M/L/Q/C/Z')
        raw = match.group()
        value = raw if raw in 'MLQCZmlqcz' else float(raw)
        if isinstance(value, float) and not math.isfinite(value):
            fail('path coordinates must be finite')
        tokens.append(value)
        last = match.end()
    if path[last:].strip(' \t\r\n,'):
        fail('path only supports M/L/Q/C/Z')
    result, index, command, moved, point, drawing = [], 0, None, False, (0., 0.), False
    while index < len(tokens):
        if isinstance(tokens[index], str):
            command = tokens[index]
            index += 1
        if not command:
            fail('path must start with M 0 0')
        kind, relative = command.upper(), command.islower()
        if kind == 'Z':
            if not moved:
                fail('path must start with M 0 0')
            result.append('Z')
            point, command, drawing = (0., 0.), None, True
            continue
        count = {'M': 2, 'L': 2, 'Q': 4, 'C': 6}[kind]
        values = tokens[index:index + count]
        if len(values) != count or any(isinstance(v, str) for v in values):
            fail('path has incomplete coordinates')
        index += count
        points = [(values[n] + (point[0] if relative else 0), values[n + 1] + (point[1] if relative else 0)) for n in range(0, count, 2)]
        if kind == 'M':
            if moved or points[0] != (0, 0):
                fail('path must start with M 0 0 and contain one subpath')
            moved = True
            command = 'l' if relative else 'L'
        elif not moved:
            fail('path must start with M 0 0')
        else:
            drawing = True
        endpoint = points[-1]
        if kind == 'Q':
            control, end = points
            points = [(point[0] + 2 / 3 * (control[0] - point[0]), point[1] + 2 / 3 * (control[1] - point[1])), (end[0] + 2 / 3 * (control[0] - end[0]), end[1] + 2 / 3 * (control[1] - end[1])), end]
            kind = 'C'
        result.append(kind)
        for x, y in points:
            result.extend((_number(x / width), _number(y / height)))
        point = endpoint
    if not moved or not drawing:
        fail('path needs a drawing segment')
    return ' '.join(result + ['E'])


def _normalize(animations, shapes, page_size):
    if isinstance(page_size, dict):
        width, height = page_size.get('width'), page_size.get('height')
    else:
        width, height = page_size
    finite = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not finite(width) or not finite(height) or width <= 0 or height <= 0:
        raise ValueError('animation page_size requires positive width and height')
    if not isinstance(animations, (list, tuple)):
        raise ValueError('animations must be an array')
    groups = []
    for index, source in enumerate(animations):
        def fail(detail):
            element_id = source.get('elementId', '?') if isinstance(source, dict) else '?'
            raise ValueError(f'animations[{index}] ({element_id}) {detail}')
        if not isinstance(source, dict) or source.get('elementId') not in shapes:
            fail('elementId must reference an existing element')
        effect = source.get('effect')
        if effect not in DURATIONS:
            fail('effect is unsupported')
        trigger, direction, easing = source.get('trigger', 'onClick'), source.get('direction', 'up'), source.get('easing', 'linear')
        if trigger not in ('onClick', 'withPrevious', 'afterPrevious'):
            fail('trigger is invalid')
        if direction not in ('up', 'down', 'left', 'right'):
            fail('direction is invalid')
        if effect.startswith('float-') and direction not in ('up', 'down'):
            fail('float direction must be up or down')
        if easing not in ('linear', 'ease-in', 'ease-out', 'ease-in-out'):
            fail('easing is invalid')
        duration = source.get('durationMs', DURATIONS[effect]) if DURATIONS[effect] else 0
        delay, repeat = source.get('delayMs', 0), source.get('repeat', 1)
        if not finite(duration) or duration < 0 or (DURATIONS[effect] and duration == 0):
            fail('durationMs must be positive')
        if not finite(delay) or delay < 0:
            fail('delayMs must be nonnegative')
        if not finite(repeat) or int(repeat) != repeat or repeat < 1:
            fail('repeat must be a positive integer')
        if effect in ('fill-color', 'color-pulse') and not re.fullmatch(r'#?[\da-fA-F]{6}', str(source.get('color', ''))):
            fail('color must be a six-digit HEX color')
        if effect == 'transparency' and (not finite(source.get('amount')) or not 0 <= source['amount'] <= 1):
            fail('amount must be between 0 and 1')
        path = _path(source.get('path'), width, height, fail) if effect == 'motion-path' else None
        if trigger == 'onClick' or not groups:
            groups.append({'automatic': trigger != 'onClick', 'items': [], 'duration': 0})
        group = groups[-1]
        previous = group['items'][-1] if group['items'] else None
        start = (previous['end'] if trigger == 'afterPrevious' else previous['start']) if previous else 0
        start += delay
        item = dict(source, trigger=trigger, direction=direction, easing=easing, duration=duration, repeat=int(repeat), start=start, end=start + duration * repeat, path=path, index=index, groupIndex=len(groups)-1)
        group['items'].append(item)
        group['duration'] = max(group['duration'], item['end'])
    return groups


def _painted_shapes(shape):
    if hasattr(shape, 'shapes'):
        for child in shape.shapes:
            yield from _painted_shapes(child)
    else:
        yield shape


def _fill_color(shape):
    """Keep theme/system colors as OOXML colors instead of guessing an RGB value."""
    color_tags = {'srgbClr', 'schemeClr', 'sysClr', 'scrgbClr', 'hslClr', 'prstClr'}
    fills = shape._element.xpath('./p:spPr/a:solidFill/* | ./p:grpSpPr/a:solidFill/* | ./p:style/a:fillRef/*')
    for color in fills:
        if etree.QName(color).localname in color_tags:
            return deepcopy(color)
    # Unstyled shapes use the theme's accent1 fill; preserve that theme reference.
    return _node(None, 'a:schemeClr', val='accent1')


class _Timing:
    def __init__(self):
        self.last_id = 0

    def ctn(self, parent, duration=None, delay=None, **attrs):
        self.last_id += 1
        node = _node(parent, 'p:cTn', id=self.last_id, dur=None if duration is None else (duration if isinstance(duration, str) else max(1, round(duration))), **attrs)
        if delay is not None:
            _node(_node(node, 'p:stCondLst'), 'p:cond', delay=delay if isinstance(delay, str) else max(0, round(delay)))
        return node

    def behavior(self, parent, shape, duration, names=(), delay=0, fill='hold', reverse=False, additive=None, background=False, context=None):
        behavior = _node(parent, 'p:cBhvr', additive=additive, rctx=context)
        ctn = self.ctn(behavior, duration, delay=delay if delay else None, fill=fill, autoRev='1' if reverse else None)
        target = _node(_node(behavior, 'p:tgtEl'), 'p:spTgt', spid=shape.shape_id)
        if background:
            _node(target, 'p:bg')
        if names:
            names_node = _node(behavior, 'p:attrNameLst')
            for name in names:
                _node(names_node, 'p:attrName').text = name
        return ctn

    def set(self, parent, shape, name, value, delay=0, duration=1, fill='hold', background=False):
        node = _node(parent, 'p:set')
        self.behavior(node, shape, duration, [name], delay, fill=fill, background=background)
        _node(_node(node, 'p:to'), 'p:strVal', val=value)

    def effect(self, parent, shape, duration, filter_name, transition='in', **kwargs):
        node = _node(parent, 'p:animEffect', transition=transition, filter=filter_name, **kwargs)
        self.behavior(node, shape, duration)

    def position(self, parent, shape, duration, name, start, end, fill='hold'):
        node = _node(parent, 'p:anim', calcmode='lin', valueType='num')
        self.behavior(node, shape, duration, [name], additive='base', fill=fill)
        values = _node(node, 'p:tavLst')
        for time, value in ((0, start), (100000, end)):
            _node(_node(_node(values, 'p:tav', tm=time), 'p:val'), 'p:strVal', val=value)

    def scale(self, parent, shape, duration, start, end, delay=0, reverse=False):
        node = _node(parent, 'p:animScale', zoomContents='1')
        self.behavior(node, shape, duration, delay=delay, reverse=reverse)
        _node(node, 'p:from', x=round(start * 100000), y=round(start * 100000))
        _node(node, 'p:to', x=round(end * 100000), y=round(end * 100000))

    def rotate(self, parent, shape, duration, start, end, delay=0):
        node = _node(parent, 'p:animRot', **{'from': round(start * 60000), 'to': round(end * 60000)})
        self.behavior(node, shape, duration, delay=delay)

    def color(self, parent, shape, duration, start, end, reverse=False):
        node = _node(parent, 'p:animClr', clrSpc='rgb', dir='cw')
        self.behavior(node, shape, duration, ['fillcolor'], reverse=reverse, background=True, fill='remove' if reverse else 'hold')
        _node(node, 'p:from').append(deepcopy(start))
        _node(_node(node, 'p:to'), 'a:srgbClr', val=end)
        # PowerPoint needs the object's fill enabled for noFill and picture fills.
        self.set(parent, shape, 'fill.type', 'solid', duration=duration * (2 if reverse else 1), background=True, fill='remove' if reverse else 'hold')
        self.set(parent, shape, 'fill.on', 'true', duration=duration * (2 if reverse else 1), background=True, fill='remove' if reverse else 'hold')


def _effects(builder, parent, item, shape, state):
    effect, duration = item['effect'], item['duration']
    direction, entrance = item['direction'], effect in ENTRANCE
    if entrance or effect in EXIT and effect != 'disappear':
        # Exit repetitions restart from a visible object on each iteration.
        builder.set(parent, shape, 'style.visibility', 'visible')
    if effect == 'appear':
        return
    if effect == 'disappear':
        builder.set(parent, shape, 'style.visibility', 'hidden')
        return
    if effect in ('fade-in', 'fade-out'):
        builder.effect(parent, shape, duration, 'fade', 'in' if entrance else 'out')
    elif effect.startswith('wipe-'):
        builder.effect(parent, shape, duration, f'wipe({direction})', 'in' if entrance else 'out')
    elif effect == 'peek-in':
        # Native slide filter moves through the object's own edge clipping mask.
        origin = {'up':'fromBottom', 'down':'fromTop', 'left':'fromRight', 'right':'fromLeft'}[direction]
        builder.effect(parent, shape, duration, f'slide({origin})')
    elif effect.startswith(('fly-', 'float-')) or effect == 'rise-in':
        if effect == 'rise-in':
            direction = 'up'
        axis = 'y' if direction in ('up', 'down') else 'x'
        original = f'#ppt_{axis}'
        if effect.startswith('float-'):
            # 10% of page height (these effects only accept vertical directions).
            sign = 1 if (direction == 'up') == entrance else -1
            distant = original + ('+' if sign > 0 else '-') + '0.1'
            builder.effect(parent, shape, duration, 'fade', 'in' if entrance else 'out')
        else:
            positive_side = (direction in ('up', 'left')) == entrance
            distant = f'1+#ppt_{"h" if axis == "y" else "w"}/2' if positive_side else f'0-#ppt_{"h" if axis == "y" else "w"}/2'
        builder.position(parent, shape, duration, f'ppt_{axis}', distant if entrance else original, original if entrance else distant, fill='hold' if entrance else 'remove')
    elif effect in ('zoom-in', 'zoom-out'):
        current = state['scale']
        builder.scale(parent, shape, duration, 0 if entrance else current, current if entrance else 0)
    elif effect == 'grow-shrink':
        builder.scale(parent, shape, duration, state['scale'], state['scale'] * 1.5)
        state['scale'] *= 1.5
    elif effect == 'pulse':
        builder.scale(parent, shape, duration / 2, state['scale'], state['scale'] * 1.1, reverse=True)
    elif effect == 'spin':
        builder.rotate(parent, shape, duration, state['rotate'], state['rotate'] + 360)
        state['rotate'] += 360
    elif effect == 'teeter':
        # Four alternating small rotations and return to the exact authored angle.
        angles = [0, 5, 0, -5, 0, 5, 0, -5, 0]
        for index, (start, end) in enumerate(zip(angles, angles[1:])):
            builder.rotate(parent, shape, duration / 8, state['rotate'] + start, state['rotate'] + end, delay=index * duration / 8)
    elif effect in ('fill-color', 'color-pulse'):
        target = item['color'].lstrip('#').upper()
        # Groups have no independent painted fill. Color their actual constituent
        # shapes, retaining a separate starting color for each; other effects target
        # the group as one object.
        for target_shape in _painted_shapes(shape):
            target_state = state['colors'].setdefault(target_shape.shape_id, _fill_color(target_shape))
            builder.color(parent, target_shape, duration / 2 if effect == 'color-pulse' else duration, target_state, target, reverse=effect == 'color-pulse')
            if effect == 'fill-color':
                state['colors'][target_shape.shape_id] = _node(None, 'a:srgbClr', val=target)
    elif effect == 'transparency':
        value = _number(item['amount'])
        # Native PowerPoint Transparency compound. A generic anim(style.opacity)
        # looks plausible in XML but is dropped by PowerPoint's slideshow engine.
        builder.set(parent, shape, 'style.opacity', value, duration=duration)
        node = _node(parent, 'p:animEffect', filter='image', prLst=f'opacity: {value}')
        builder.behavior(node, shape, duration, context='IE')
    elif effect == 'motion-path':
        node = _node(parent, 'p:animMotion', origin='layout', path=item['path'], pathEditMode='relative')
        builder.behavior(node, shape, duration, ['ppt_x', 'ppt_y'], additive='sum')
    if effect in EXIT:
        # PowerPoint uses 1 ms sets for visibility. Keep the complete final
        # set inside the effect so repeats and following entrances share the
        # declared end boundary (at most one millisecond of quantization).
        builder.set(parent, shape, 'style.visibility', 'hidden', delay=max(0,round(duration)-1))


def apply_animations(slide, animations, element_shapes, page_size):
    """Atomically replace slide timing and return a JSON-serializable export audit.

    element_shapes maps stable PPTD IDs to actual python-pptx shape/group objects.
    page_size uses the document's original px units (not EMUs). Empty animations
    remove an existing timing tree. Validation failure leaves the slide unchanged.
    """
    groups = _normalize(animations, element_shapes, page_size)
    builder = _Timing()
    timing = _node(None, 'p:timing')
    root = builder.ctn(_node(_node(timing, 'p:tnLst'), 'p:par'), 'indefinite', restart='never', nodeType='tmRoot')
    root_children = _node(root, 'p:childTnLst')
    # Explicit initialization means playback doesn't depend on a guessed preset ID.
    seen = set()
    for item in (item for group in groups for item in group['items']):
        element_id = item['elementId']
        if element_id not in seen and item['effect'] in ENTRANCE:
            builder.set(root_children, element_shapes[element_id], 'style.visibility', 'hidden')
        seen.add(element_id)
    sequence = _node(root_children, 'p:seq', concurrent='1', nextAc='seek')
    main = builder.ctn(sequence, 'indefinite', nodeType='mainSeq')
    main_children = _node(main, 'p:childTnLst')
    states, audit = {}, []
    for group_index, group in enumerate(groups):
        group_node = builder.ctn(_node(main_children, 'p:par'), None, delay=0 if group['automatic'] else 'indefinite', fill='hold')
        group_children = _node(group_node, 'p:childTnLst')
        for item in group['items']:
            shape = element_shapes[item['elementId']]
            state = states.setdefault(item['elementId'], {'scale': 1., 'rotate': float(shape.rotation), 'colors': {}})
            node_type = {'onClick': 'clickEffect', 'withPrevious': 'withEffect', 'afterPrevious': 'afterEffect'}[item['trigger']]
            effect_class = 'entr' if item['effect'] in ENTRANCE else 'exit' if item['effect'] in EXIT else 'path' if item['effect'] == 'motion-path' else 'emph'
            easing = 'linear' if item['effect'] == 'rise-in' else item['easing']
            accel, decel = {'linear': (0,0), 'ease-in': (100000,0), 'ease-out': (0,100000), 'ease-in-out': (50000,50000)}[easing]
            # Duration and repeatCount belong to the effect container; nested
            # behaviors inherit repetition without multiplying it twice.
            effect_node = builder.ctn(_node(group_children, 'p:par'), item['duration'], delay=item['start'], fill='hold', nodeType=node_type, presetClass=effect_class, repeatCount=item['repeat'] * 1000 if item['repeat'] > 1 else None, accel=accel or None, decel=decel or None)
            _effects(builder, _node(effect_node, 'p:childTnLst'), item, shape, state)
            audit.append({'elementId': item['elementId'], 'effect': item['effect'], 'groupIndex': group_index, 'startMs': item['start'], 'durationMs': item['duration'], 'repeat': item['repeat'], 'shapeId': shape.shape_id})
    for name, event in (('prevCondLst', 'onPrev'), ('nextCondLst', 'onNext')):
        condition = _node(_node(sequence, f'p:{name}'), 'p:cond', evt=event, delay='0')
        _node(_node(condition, 'p:tgtEl'), 'p:sldTgt')
    # p:timing appears before extLst and after clrMapOvr / transition in CT_Slide.
    old = slide._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}timing')
    if old is not None:
        slide._element.remove(old)
    if groups:
        extension = slide._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}extLst')
        if extension is not None:
            slide._element.insert(list(slide._element).index(extension), timing)
        else:
            slide._element.append(timing)
    return {'animationCount': len(audit), 'groupCount': len(groups), 'animations': audit, 'verification': 'ooxml-structure'}
