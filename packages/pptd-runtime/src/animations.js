/** PPTD v2 page animations. The source document is never mutated.
 * Explicit RAF scheduling makes cancellation and sequential persistent effects deterministic.
 * createAnimationPlayer(root, animations, {pageSize:[width,height], autoPlay:true})
 * automatically starts a first withPrevious/afterPrevious group. play() starts/replays;
 * next() completes an active group and starts the next click group; reset()/destroy()
 * restore every inline style touched by the player. reset() waits for play()/next().
 */
const DURATIONS = Object.freeze({
  appear: 0, 'fade-in': 500, 'fly-in': 500, 'zoom-in': 500, 'wipe-in': 500,
  'float-in': 500, 'peek-in': 500, 'rise-in': 1000, pulse: 600,
  'grow-shrink': 2000, spin: 2000, teeter: 1000, 'fill-color': 2000,
  transparency: 2000, 'color-pulse': 2000, disappear: 0, 'fade-out': 500,
  'fly-out': 500, 'zoom-out': 500, 'wipe-out': 500, 'float-out': 500, 'motion-path': 2000,
});
const ENTRANCE = new Set(['appear', 'fade-in', 'fly-in', 'zoom-in', 'wipe-in', 'float-in', 'peek-in', 'rise-in']);
const EXIT = new Set(['disappear', 'fade-out', 'fly-out', 'zoom-out', 'wipe-out', 'float-out']);
const NUMBER = /[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:e[-+]?\d+)?/iy;

function parsePath(path, fail) {
  if (typeof path !== 'string') fail('path is required');
  const tokens = []; let i = 0;
  while (i < path.length) {
    if (/[\s,]/.test(path[i])) { i++; continue; }
    if (/[MLQCZmlqcz]/.test(path[i])) { tokens.push(path[i++]); continue; }
    NUMBER.lastIndex = i;
    const match = NUMBER.exec(path);
    if (!match || !Number.isFinite(Number(match[0]))) fail('path only supports M/L/Q/C/Z and finite coordinates');
    tokens.push(Number(match[0])); i = NUMBER.lastIndex;
  }
  const segments = []; let current = [0, 0], command, moved = false;
  for (i = 0; i < tokens.length;) {
    if (typeof tokens[i] === 'string') command = tokens[i++];
    if (!command) fail('path must start with M 0 0');
    const kind = command.toUpperCase(), relative = command !== kind;
    if (kind === 'Z') { if (!moved) fail('path must start with M 0 0'); segments.push({ kind: 'L', points: [current, [0, 0]] }); current = [0, 0]; command = null; continue; }
    const count = { M: 2, L: 2, Q: 4, C: 6 }[kind];
    const values = tokens.slice(i, i + count);
    if (values.length !== count || values.some(value => typeof value !== 'number')) fail('path has incomplete coordinates');
    i += count;
    const points = [];
    for (let j = 0; j < count; j += 2) points.push([values[j] + (relative ? current[0] : 0), values[j + 1] + (relative ? current[1] : 0)]);
    if (kind === 'M') {
      if (moved || points[0][0] !== 0 || points[0][1] !== 0) fail('path must start with M 0 0 and contain one subpath');
      moved = true; command = relative ? 'l' : 'L';
    } else {
      if (!moved) fail('path must start with M 0 0');
      segments.push({ kind, points: [current, ...points] });
    }
    current = points.at(-1);
  }
  if (!moved || !segments.length) fail('path needs a drawing segment');
  // Sample Beziers at equal parameter intervals, then interpolate by arc length.
  const samples = [{ point: [0, 0], distance: 0 }]; let length = 0;
  for (const segment of segments) {
    const steps = segment.kind === 'L' ? 1 : 128;
    for (let n = 1; n <= steps; n++) {
      const t = n / steps, u = 1 - t, p = segment.points;
      const point = [0, 1].map(axis => segment.kind === 'L' ? u * p[0][axis] + t * p[1][axis] : segment.kind === 'Q' ? u * u * p[0][axis] + 2 * u * t * p[1][axis] + t * t * p[2][axis] : u ** 3 * p[0][axis] + 3 * u * u * t * p[1][axis] + 3 * u * t * t * p[2][axis] + t ** 3 * p[3][axis]);
      const prior = samples.at(-1).point;
      length += Math.hypot(point[0] - prior[0], point[1] - prior[1]); samples.push({ point, distance: length });
    }
  }
  return progress => {
    const target = progress * length;
    let low = 0, high = samples.length - 1;
    while (low + 1 < high) { const mid = (low + high) >> 1; if (samples[mid].distance < target) low = mid; else high = mid; }
    const a = samples[low], b = samples[high], ratio = b.distance === a.distance ? 1 : (target - a.distance) / (b.distance - a.distance);
    return a.point.map((value, axis) => value + (b.point[axis] - value) * ratio);
  };
}

// Constant acceleration/deceleration matches OOXML cTn accel/decel. CSS's named
// cubic-Bezier timing functions have different curves, so use this in both adapters.
function ease(t, easing) {
  if (easing === 'ease-in') return t * t;
  if (easing === 'ease-out') return 1 - (1 - t) ** 2;
  if (easing === 'ease-in-out') return t < 0.5 ? 2 * t * t : 1 - 2 * (1 - t) ** 2;
  return t;
}
function rgba(value) {
  const rgb = value.match(/[\d.]+/g)?.map(Number);
  if (rgb?.length >= 3) return [rgb[0], rgb[1], rgb[2], rgb[3] ?? 1];
  return [0, 0, 0, 0];
}
function mixColor(from, to, t) {
  const value = from.map((n, index) => n + (to[index] - n) * t);
  return `rgba(${Math.round(value[0])}, ${Math.round(value[1])}, ${Math.round(value[2])}, ${value[3]})`;
}

export function createAnimationPlayer(root, animations = [], options = {}) {
  if (!root?.querySelectorAll) throw new TypeError('animation root must be a DOM element');
  if (!Array.isArray(animations)) throw new TypeError('animations must be an array');
  const size = options.pageSize ?? [root.clientWidth, root.clientHeight];
  const width = Number(Array.isArray(size) ? size[0] : size.width), height = Number(Array.isArray(size) ? size[1] : size.height);
  if (!(width > 0 && height > 0 && Number.isFinite(width) && Number.isFinite(height))) throw new Error('animation pageSize must have a positive width and height');
  const view = root.ownerDocument.defaultView;
  const clock = options.clock ?? { now: () => view.performance.now(), requestAnimationFrame: cb => view.requestAnimationFrame(cb), cancelAnimationFrame: id => view.cancelAnimationFrame(id) };
  const nodes = new Map([...root.querySelectorAll('[data-element-id]')].map(node => [node.dataset.elementId, node]));
  const groups = [];
  // Validate everything before touching the DOM.
  for (const [index, input] of animations.entries()) {
    const fail = detail => { throw new Error(`animations[${index}] (${input?.elementId ?? '?'}) ${detail}`); };
    if (!input || !nodes.has(input.elementId)) fail('elementId must reference an existing element');
    if (!Object.hasOwn(DURATIONS, input.effect)) fail('effect is unsupported');
    const trigger = input.trigger ?? 'onClick', direction = input.direction ?? 'up', easing = input.easing ?? 'linear';
    if (!['onClick', 'withPrevious', 'afterPrevious'].includes(trigger)) fail('trigger is invalid');
    if (!['up', 'down', 'left', 'right'].includes(direction)) fail('direction is invalid');
    if (input.effect.startsWith('float-') && !['up', 'down'].includes(direction)) fail('float direction must be up or down');
    if (!['linear', 'ease-in', 'ease-out', 'ease-in-out'].includes(easing)) fail('easing is invalid');
    const duration = DURATIONS[input.effect] === 0 ? 0 : (input.durationMs ?? DURATIONS[input.effect]);
    const delay = input.delayMs ?? 0, repeat = input.repeat ?? 1;
    if (!Number.isFinite(duration) || duration < 0 || (DURATIONS[input.effect] && duration === 0)) fail('durationMs must be positive');
    if (!Number.isFinite(delay) || delay < 0) fail('delayMs must be nonnegative');
    if (!Number.isInteger(repeat) || repeat < 1) fail('repeat must be a positive integer');
    if (['fill-color', 'color-pulse'].includes(input.effect) && !/^#?[\da-fA-F]{6}$/.test(input.color ?? '')) fail('color must be a six-digit HEX color');
    if (input.effect === 'transparency' && (!Number.isFinite(input.amount) || input.amount < 0 || input.amount > 1)) fail('amount must be between 0 and 1');
    const path = input.effect === 'motion-path' ? parsePath(input.path, fail) : null;
    if (trigger === 'onClick' || !groups.length) groups.push({ automatic: trigger !== 'onClick', items: [], duration: 0 });
    const group = groups.at(-1), previous = group.items.at(-1);
    const start = (previous ? (trigger === 'afterPrevious' ? previous.end : previous.start) : 0) + delay;
    const item = { ...input, index, trigger, direction, easing, duration, delay, repeat, path, start, end: start + duration * repeat, started: false, finished: false };
    group.items.push(item); group.duration = Math.max(group.duration, item.end);
  }
  const snapshots = new Map(), states = new Map();
  const save = node => { if (!snapshots.has(node)) snapshots.set(node, node.getAttribute('style')); };
  const affected = new Set(animations.map(animation => animation.elementId));
  for (const id of affected) {
    const node = nodes.get(id), computed = view.getComputedStyle(node);
    save(node);
    const marked = [...node.querySelectorAll('[data-animation-fill]')];
    let fills = marked.length ? marked : [...node.querySelectorAll('svg path,svg rect,svg ellipse,svg circle,svg polygon,svg polyline')].filter(target => !target.closest('defs,clipPath,mask') && view.getComputedStyle(target).fill !== 'none');
    if (computed.backgroundColor !== 'rgba(0, 0, 0, 0)' || !fills.length) fills = [node];
    const targets = fills.map(target => {
      save(target);
      const property = target.namespaceURI === 'http://www.w3.org/2000/svg' ? 'fill' : 'backgroundColor';
      return { node: target, property, initial: rgba(view.getComputedStyle(target)[property]) };
    });
    states.set(id, { node, targets, initial: { opacity: Number(computed.opacity), transform: computed.transform === 'none' ? '' : computed.transform, clip: computed.clipPath, visibility: computed.visibility }, x: 0, y: 0, scale: 1, rotate: 0, opacity: Number(computed.opacity), colors: targets.map(target => [...target.initial]) });
  }
  let groupIndex = -1, running = false, destroyed = false, frame = null, origin = 0, elapsed = 0, completed = groups.length === 0;
  const status = () => ({ state: destroyed ? 'destroyed' : running ? 'playing' : completed ? 'completed' : 'ready', groupIndex, groupCount: groups.length, playing: running, completed });
  const notify = () => options.onStatusChange?.(status());
  const transform = state => { state.node.style.transform = `translate(${state.x}px,${state.y}px) ${state.initial.transform} rotate(${state.rotate}deg) scale(${state.scale})`; };
  const restore = () => { for (const [node, style] of snapshots) { if (style === null) node.removeAttribute('style'); else node.setAttribute('style', style); } };
  const hideEntrances = () => {
    const seen = new Set();
    for (const animation of animations) { if (!seen.has(animation.elementId) && ENTRANCE.has(animation.effect)) nodes.get(animation.elementId).style.visibility = 'hidden'; seen.add(animation.elementId); }
  };
  const render = (item, progress, final) => {
    const state = states.get(item.elementId), node = state.node, base = item.base, effect = item.effect;
    const t = ease(progress, effect === 'rise-in' ? 'linear' : item.easing), pulse = 1 - Math.abs(2 * t - 1);
    const opacity = value => { state.opacity = value; node.style.opacity = String(value); };
    const position = (x, y) => { state.x = x; state.y = y; transform(state); };
    const scale = value => { state.scale = value; transform(state); };
    if (ENTRANCE.has(effect)) node.style.visibility = 'visible';
    if (effect === 'appear') node.style.visibility = 'visible';
    else if (effect === 'disappear') node.style.visibility = 'hidden';
    else if (effect === 'fade-in' || effect === 'fade-out') opacity(base.opacity * (effect === 'fade-in' ? t : 1 - t));
    else if (effect === 'transparency') opacity(base.opacity + (item.amount - base.opacity) * t);
    else if (effect === 'pulse' || effect === 'grow-shrink') scale(base.scale * (1 + (effect === 'pulse' ? 0.1 * pulse : 0.5 * t)));
    else if (effect === 'spin' || effect === 'teeter') { state.rotate = base.rotate + (effect === 'spin' ? 360 * t : Math.sin(t * Math.PI * 4) * 5); transform(state); }
    else if (effect === 'zoom-in' || effect === 'zoom-out') scale(base.scale * (effect === 'zoom-in' ? t : 1 - t));
    else if (effect === 'motion-path') { const [x, y] = item.path(t); position(base.x + x, base.y + y); }
    else if (effect === 'fill-color' || effect === 'color-pulse') {
      const hex = item.color.replace('#', ''), target = [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16), 1];
      const fraction = effect === 'color-pulse' ? pulse : t;
      state.targets.forEach((targetNode, index) => { state.colors[index] = base.colors[index].map((value, channel) => value + (target[channel] - value) * fraction); targetNode.node.style[targetNode.property] = mixColor(base.colors[index], target, fraction); });
    } else if (effect.startsWith('wipe-')) {
      const hidden = 100 * (effect === 'wipe-in' ? 1 - t : t);
      // up reveals from the bottom and erases from the bottom; right is analogous.
      const side = effect === 'wipe-in' ? { up: 0, down: 2, left: 3, right: 1 }[item.direction] : { up: 2, down: 0, left: 1, right: 3 }[item.direction];
      const inset = [0, 0, 0, 0]; inset[side] = hidden; node.style.clipPath = `inset(${inset.map(value => `${value}%`).join(' ')})`;
    } else {
      const entrance = ENTRANCE.has(effect), factor = entrance ? 1 - t : t;
      let direction = effect === 'rise-in' ? 'up' : item.direction;
      const vector = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] }[direction];
      let distance;
      if (effect.startsWith('float-')) distance = height * 0.1;
      else if (effect === 'peek-in') distance = direction === 'up' || direction === 'down' ? node.offsetHeight : node.offsetWidth;
      else {
        // Coordinates are in page px even when the preview is CSS-scaled.
        const x = (parseFloat(node.style.left) || node.offsetLeft) + base.x, y = (parseFloat(node.style.top) || node.offsetTop) + base.y;
        const w = parseFloat(node.style.width) || node.offsetWidth, h = parseFloat(node.style.height) || node.offsetHeight;
        distance = entrance ? { up: height - y, down: y + h, left: width - x, right: x + w }[direction] : { up: y + h, down: height - y, left: x + w, right: width - x }[direction];
      }
      const sign = entrance ? -1 : 1;
      position(base.x + vector[0] * distance * factor * sign, base.y + vector[1] * distance * factor * sign);
      if (effect.startsWith('float-')) opacity(base.opacity * (entrance ? t : 1 - t));
      if (effect === 'peek-in') {
        // CSS clip-path is attached to the translated node, so clip the displaced
        // part against its original bounds using the opposite inset edge.
        const side = { up: 2, down: 0, left: 1, right: 3 }[direction];
        const inset = [0, 0, 0, 0]; inset[side] = (1 - t) * 100; node.style.clipPath = `inset(${inset.map(value => `${value}%`).join(' ')})`;
      }
    }
    if (final) {
      if (EXIT.has(effect)) {
        node.style.visibility = 'hidden';
        // An exit hides the object; temporary travel/fade/scale must not corrupt
        // the starting state of a later entrance of the same object.
        state.x = base.x; state.y = base.y; state.scale = base.scale; opacity(base.opacity); transform(state);
      }
      if (effect.startsWith('wipe-') || effect === 'peek-in') node.style.clipPath = state.initial.clip;
    }
  };
  const startItem = item => {
    const state = states.get(item.elementId);
    item.base = { x: state.x, y: state.y, scale: state.scale, rotate: state.rotate, opacity: state.opacity, colors: state.colors.map(color => [...color]) };
    item.started = true;
  };
  const advance = target => {
    const group = groups[groupIndex];
    // Visit every crossed start/end boundary before sampling the current frame.
    // This preserves captured colors/transforms after suspended tabs or large frames.
    const boundaries = [...new Set(group.items.flatMap(item => [item.start, item.end]).filter(time => time >= elapsed && time <= target))].sort((a, b) => a - b);
    const sample = time => {
      for (const item of group.items) {
        if (item.finished || time < item.start) continue;
        if (!item.started) startItem(item);
        const final = time >= item.end;
        const progress = final || item.duration === 0 ? 1 : ((time - item.start) % item.duration) / item.duration;
        render(item, progress, final); item.finished = final;
      }
    };
    boundaries.forEach(sample); sample(target); elapsed = target;
    if (target >= group.duration) { running = false; completed = groupIndex === groups.length - 1; notify(); }
  };
  const loop = now => { frame = null; if (!running || destroyed) return; advance(Math.min(now - origin, groups[groupIndex].duration)); if (running) frame = clock.requestAnimationFrame(loop); };
  const cancel = () => { if (frame !== null) clock.cancelAnimationFrame(frame); frame = null; };
  const startGroup = index => {
    if (index >= groups.length) return false;
    groupIndex = index; elapsed = 0; origin = clock.now(); running = true; completed = false;
    advance(0); if (running) frame = clock.requestAnimationFrame(loop); notify(); return true;
  };
  const reset = () => {
    cancel(); running = false; groupIndex = -1; completed = groups.length === 0; restore();
    for (const state of states.values()) Object.assign(state, { x: 0, y: 0, scale: 1, rotate: 0, opacity: state.initial.opacity, colors: state.targets.map(target => [...target.initial]) });
    for (const group of groups) for (const item of group.items) { item.started = false; item.finished = false; delete item.base; }
    notify();
  };
  const api = {
    play() { if (destroyed) return false; reset(); hideEntrances(); return startGroup(0); },
    next() { if (destroyed) return false; if (running) { cancel(); advance(groups[groupIndex].duration); } if (groupIndex === -1) hideEntrances(); return startGroup(groupIndex + 1); },
    reset() { if (!destroyed) reset(); },
    destroy() { if (!destroyed) { reset(); destroyed = true; notify(); } },
    get status() { return status(); },
  };
  hideEntrances();
  if (options.autoPlay !== false && groups[0]?.automatic) startGroup(0);
  return api;
}
