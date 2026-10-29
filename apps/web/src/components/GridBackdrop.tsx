import { useEffect, useRef } from 'react';

/* Animated replacement for the old two-gradient CSS grid on the home page.
   At rest it draws the exact same 32px lattice. The cursor doesn't light the
   grid directly — it drags a short chain of lagging, drifting nodes behind it,
   and those are what deposit light. That indirection is the whole point: the
   glow trails the pointer like smoke instead of snapping to it, and it keeps
   curling on its own once the pointer stops.

   Everything sticks to canvas 2D primitives that shipped well before Chrome 94
   — no roundRect, no ctx.reset(), no CSS color functions. */

const CELL = 32;
const BASE_RADIUS = 150; // influence of the lead node, css px
const ATTACK_MS = 130; // how fast a star fills toward its target
const DECAY_MS = 620; // how slowly it dissipates
const MIN_ENERGY = 0.012;
const IDLE_RAMP_MS = 900; // pointer-still → fully "drifting" state
const AMBIENT_COUNT = 16;
const AMBIENT_PEAK = 0.34;
const TAU = Math.PI * 2;

/* The chain the pointer drags. Each node chases the one ahead of it, so lag
   compounds down the tail and a direction change makes the trail curl rather
   than snap. Wander is a lissajous drift applied on top, largest at the tail. */
const TRAIL = [
  { lag: 150, radius: 1.0, weight: 1.0, wander: 7 },
  { lag: 230, radius: 0.86, weight: 0.74, wander: 14 },
  { lag: 340, radius: 0.7, weight: 0.5, wander: 22 },
  { lag: 480, radius: 0.56, weight: 0.3, wander: 30 },
];

type RGB = [number, number, number];

const LINE: RGB = [230, 227, 214]; // --line-2
const LINE_LIT: RGB = [214, 205, 182]; // warmer paper, for the halo on the lattice
/* All three stay inside the accent's own warm family — just deeper and lighter
   siblings of it. Mixing in a real gold and a teal made the field read as
   scattered confetti against the cream paper; the variation has to come from
   depth, not from hue. Sparkle is carried by the white ignition flash instead. */
const TINTS: RGB[] = [
  [210, 58, 31], // --accent
  [186, 66, 36], // deeper, slightly browner
  [226, 100, 58], // lighter, warmer
];

type Star = {
  col: number;
  row: number;
  energy: number;
  target: number;
  phase: number; // per-star breathing offset
  tint: number;
  flash: number; // short white spike when it first ignites
  lit: boolean;
};

type Ambient = {
  col: number;
  row: number;
  t: number;
  life: number;
  phase: number;
  tint: number;
};

/* Colours are quantised into per-tint ramps so the hot loop reuses strings
   instead of building an rgb() literal per star per frame. */
const STEPS = 24;
const RAMPS: string[][] = TINTS.map((tint) => {
  const ramp: string[] = [];
  for (let i = 0; i < STEPS; i += 1) {
    const t = i / (STEPS - 1);
    const r = Math.round(LINE[0] + (tint[0] - LINE[0]) * t);
    const g = Math.round(LINE[1] + (tint[1] - LINE[1]) * t);
    const b = Math.round(LINE[2] + (tint[2] - LINE[2]) * t);
    ramp.push('rgb(' + r + ',' + g + ',' + b + ')');
  }
  return ramp;
});

const BASE_LINE = 'rgba(' + LINE[0] + ',' + LINE[1] + ',' + LINE[2] + ',0.6)';
const HALO_RGB = LINE_LIT[0] + ',' + LINE_LIT[1] + ',' + LINE_LIT[2];
const FLASH = 'rgb(255,251,242)';

/* Stable per-intersection noise. Without it every point lights to exactly the
   falloff curve and the trail is a tidy geometric blob; with it the same
   intersection always gets the same character, so the field stays coherent
   frame to frame instead of fizzing. */
function hash01(col: number, row: number, salt: number) {
  const h = Math.sin(col * 127.1 + row * 311.7 + salt * 74.7) * 43758.5453;
  return h - Math.floor(h);
}

function pickTint(col: number, row: number) {
  const h = hash01(col, row, 5);
  if (h > 0.78) return 2;
  if (h > 0.5) return 1;
  return 0;
}

export function GridBackdrop() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const host = canvas?.parentElement;
    if (!canvas || !host) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let dpr = 1;
    /* 1px strokes only land on a whole device pixel at odd ratios if we nudge
       them onto the half-pixel; at dpr 2 an integer coordinate is already
       exact. */
    let half = 0.5;

    const measure = () => {
      const rect = host.getBoundingClientRect();
      width = Math.max(1, Math.round(rect.width));
      height = Math.max(1, Math.round(rect.height));
      dpr = Math.min(2, window.devicePixelRatio || 1);
      half = dpr % 2 === 1 ? 0.5 : 0;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const drawGrid = () => {
      ctx.clearRect(0, 0, width, height);
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1;
      ctx.strokeStyle = BASE_LINE;
      ctx.beginPath();
      for (let x = 0; x <= width; x += CELL) {
        ctx.moveTo(x + half, 0);
        ctx.lineTo(x + half, height);
      }
      for (let y = 0; y <= height; y += CELL) {
        ctx.moveTo(0, y + half);
        ctx.lineTo(width, y + half);
      }
      ctx.stroke();
    };

    measure();

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (reduced.matches) {
      drawGrid();
      const ro = new ResizeObserver(() => {
        measure();
        drawGrid();
      });
      ro.observe(host);
      return () => ro.disconnect();
    }

    /* Only intersections with energy live in this map. A 1700x900 viewport has
       ~1500 intersections but the trail can only reach a few hundred, so
       per-frame work stays proportional to what is actually lit. */
    const stars = new Map<number, Star>();
    const ambient: Ambient[] = [];

    const maxCol = () => Math.floor(width / CELL);
    const maxRow = () => Math.floor(height / CELL);

    const respawn = (a: Ambient) => {
      a.col = Math.floor(Math.random() * (maxCol() + 1));
      a.row = Math.floor(Math.random() * (maxRow() + 1));
      a.t = 0;
      a.life = 1900 + Math.random() * 1600;
      a.phase = Math.random() * TAU;
      a.tint = pickTint(a.col, a.row);
    };
    for (let i = 0; i < AMBIENT_COUNT; i += 1) {
      const a: Ambient = { col: 0, row: 0, t: 0, life: 0, phase: 0, tint: 0 };
      respawn(a);
      // Stagger the first cycle so they don't all peak on the same frame.
      a.t = Math.random() * a.life;
      ambient.push(a);
    }

    const trail = TRAIL.map(() => ({ x: -9999, y: -9999, dx: 0, dy: 0 }));
    let seeded = false;

    let rect = canvas.getBoundingClientRect();
    let pointerX = -9999;
    let pointerY = -9999;
    let pointerLive = false;
    let lastMoveAt = -IDLE_RAMP_MS;

    const refreshRect = () => {
      rect = canvas.getBoundingClientRect();
    };

    const onPointerMove = (e: PointerEvent) => {
      const nx = e.clientX - rect.left;
      const ny = e.clientY - rect.top;
      // Sub-pixel jitter shouldn't count as movement, or the trail never settles.
      if (Math.abs(nx - pointerX) > 0.5 || Math.abs(ny - pointerY) > 0.5) {
        lastMoveAt = performance.now();
      }
      pointerX = nx;
      pointerY = ny;
      pointerLive =
        nx >= -BASE_RADIUS &&
        ny >= -BASE_RADIUS &&
        nx <= width + BASE_RADIUS &&
        ny <= height + BASE_RADIUS;
      if (pointerLive && !seeded) {
        // First sighting: drop the whole chain on the cursor so it doesn't come
        // flying in from the corner.
        for (let i = 0; i < trail.length; i += 1) {
          trail[i].x = nx;
          trail[i].y = ny;
        }
        seeded = true;
      }
    };
    const onPointerOut = (e: PointerEvent) => {
      if (e.relatedTarget) return;
      pointerLive = false;
      // Re-seed on the way back in, or leaving at one edge and returning at
      // another would drag a lit band right across the page.
      seeded = false;
    };

    const inject = (cx: number, cy: number, radius: number, weight: number) => {
      const c0 = Math.max(0, Math.floor((cx - radius) / CELL));
      const c1 = Math.min(maxCol(), Math.floor((cx + radius) / CELL));
      const r0 = Math.max(0, Math.floor((cy - radius) / CELL));
      const r1 = Math.min(maxRow(), Math.floor((cy + radius) / CELL));
      for (let col = c0; col <= c1; col += 1) {
        const dx = col * CELL - cx;
        for (let row = r0; row <= r1; row += 1) {
          const dy = row * CELL - cy;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist > radius) continue;
          /* Between linear and quadratic falloff. Pure quadratic looked right
             when stars snapped to their target, but with an eased attack a fast
             pass never fills them and the whole field washed out — this keeps
             the off-centre stars bright enough to survive the lag. */
          const f = 1 - dist / radius;
          const shaped = f * (0.4 + 0.6 * f);
          const bias = 0.72 + hash01(col, row, 1) * 0.28;
          const target = Math.min(1, shaped * 1.7 * weight * bias);
          const key = col * 100000 + row;
          const found = stars.get(key);
          if (!found) {
            stars.set(key, {
              col,
              row,
              energy: 0,
              target,
              phase: hash01(col, row, 2) * TAU,
              tint: pickTint(col, row),
              flash: 0,
              lit: false,
            });
          } else if (target > found.target) {
            found.target = target;
          }
        }
      }
    };

    const drawHalo = (cx: number, cy: number, radius: number) => {
      // A radial gradient as the stroke style gives the falloff for free, so we
      // just re-stroke the lattice inside the lead node's bounding box.
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
      g.addColorStop(0, 'rgba(' + HALO_RGB + ',0.9)');
      g.addColorStop(1, 'rgba(' + HALO_RGB + ',0)');
      const left = cx - radius;
      const right = cx + radius;
      const top = cy - radius;
      const bottom = cy + radius;
      ctx.globalAlpha = 1;
      ctx.strokeStyle = g;
      ctx.lineWidth = 1;
      ctx.beginPath();
      const c0 = Math.max(0, Math.floor(left / CELL));
      const c1 = Math.min(maxCol(), Math.floor(right / CELL));
      for (let col = c0; col <= c1; col += 1) {
        const x = col * CELL + half;
        ctx.moveTo(x, Math.max(0, top));
        ctx.lineTo(x, Math.min(height, bottom));
      }
      const r0 = Math.max(0, Math.floor(top / CELL));
      const r1 = Math.min(maxRow(), Math.floor(bottom / CELL));
      for (let row = r0; row <= r1; row += 1) {
        const y = row * CELL + half;
        ctx.moveTo(Math.max(0, left), y);
        ctx.lineTo(Math.min(width, right), y);
      }
      ctx.stroke();
    };

    const drawStar = (col: number, row: number, e: number, tint: number, flash: number) => {
      if (e < 0.02) return;
      const x = col * CELL + half;
      const y = row * CELL + half;
      const color = RAMPS[tint][Math.min(STEPS - 1, Math.round(e * (STEPS - 1)))];

      // Soft bloom under the brighter stars — a plain low-alpha disc, because a
      // per-star radial gradient would mean hundreds of gradient objects a frame.
      if (e > 0.35) {
        ctx.globalAlpha = 0.035 + 0.07 * e;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, y, 3 + 8 * e, 0, TAU);
        ctx.fill();
      }

      ctx.globalAlpha = 0.14 + 0.78 * e;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, 0.85 + 2.1 * e, 0, TAU);
      ctx.fill();

      if (e > 0.45) {
        // The cross is what reads as a "star" rather than a dot.
        const len = 3 + 9 * e;
        ctx.globalAlpha = ((e - 0.45) / 0.55) * 0.5;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x - len, y);
        ctx.lineTo(x + len, y);
        ctx.moveTo(x, y - len);
        ctx.lineTo(x, y + len);
        ctx.stroke();
      }

      if (flash > 0.02) {
        ctx.globalAlpha = flash * 0.55;
        ctx.fillStyle = FLASH;
        ctx.beginPath();
        ctx.arc(x, y, 0.6 + 1.5 * e, 0, TAU);
        ctx.fill();
      }
    };

    let raf = 0;
    let last = performance.now();

    const frame = (now: number) => {
      // rAF hands us the frame's start time, which can predate the timestamp
      // start() captured — clamp both ends so nothing runs backwards.
      const dt = Math.max(0, Math.min(50, now - last));
      last = now;

      /* One slow sine drives the whole field: it widens and brightens on the
         inhale, narrows and dims on the exhale. ~5.5s, well under a resting
         breath rate so it registers as atmosphere rather than a pulse. */
      const breath = 0.5 + 0.5 * Math.sin(now * 0.001142);
      const radiusMul = 0.86 + 0.2 * breath;
      const brightMul = 0.84 + 0.22 * breath;
      // 0 while the pointer is moving, 1 once it has been still for a beat.
      const idle = Math.min(1, Math.max(0, (now - lastMoveAt) / IDLE_RAMP_MS));

      /* Targets are rebuilt from scratch every frame: a star is either under a
         trail node right now (attack toward what that node asks for) or it is
         not (release). Letting the target decay alongside the energy instead
         meant stars chased a receding goal and never reached full brightness. */
      stars.forEach((star) => {
        star.target = 0;
      });

      if (pointerLive && seeded) {
        for (let i = 0; i < trail.length; i += 1) {
          const node = trail[i];
          const spec = TRAIL[i];
          const tx = i === 0 ? pointerX : trail[i - 1].x;
          const ty = i === 0 ? pointerY : trail[i - 1].y;
          // Exponential chase: frame-rate independent, and the compounding lag
          // down the chain is what turns a flick into a curl.
          const k = 1 - Math.exp(-dt / spec.lag);
          node.x += (tx - node.x) * k;
          node.y += (ty - node.y) * k;

          /* Two incommensurate sines per axis approximate a slow curl. The
             amplitude nearly doubles once the pointer stops, so a parked cursor
             still has smoke turning over inside it. */
          const amp = spec.wander * (0.55 + 0.8 * idle);
          node.dx =
            Math.sin(now * 0.00063 + i * 1.7) * amp + Math.sin(now * 0.00121 + i * 3.1) * amp * 0.45;
          node.dy =
            Math.cos(now * 0.00051 + i * 2.3) * amp + Math.cos(now * 0.00098 + i * 0.7) * amp * 0.45;

          inject(
            node.x + node.dx,
            node.y + node.dy,
            BASE_RADIUS * spec.radius * radiusMul,
            spec.weight,
          );
        }
      }

      const rise = 1 - Math.exp(-dt / ATTACK_MS);
      const fall = Math.exp(-dt / DECAY_MS);
      const flashFall = Math.exp(-dt / 210);

      drawGrid();

      if (pointerLive && seeded) {
        drawHalo(trail[0].x + trail[0].dx, trail[0].y + trail[0].dy, BASE_RADIUS * radiusMul);
      }

      stars.forEach((star, key) => {
        /* Easing the *rise* as well as the fall is what kills the rigid
           follow-the-mouse feel — a star fills like ink spreading, it doesn't
           snap on the frame the cursor reaches it. */
        if (star.target > star.energy) star.energy += (star.target - star.energy) * rise;
        else star.energy *= fall;
        star.flash *= flashFall;

        if (!star.lit && star.energy > 0.5) {
          star.lit = true;
          star.flash = 1;
        } else if (star.lit && star.energy < 0.35) {
          star.lit = false;
        }

        if (star.energy < MIN_ENERGY) {
          stars.delete(key);
          return;
        }
        // Per-star shimmer, offset by its hash so neighbours never pulse in step.
        const shimmer = 0.85 + 0.15 * Math.sin(now * 0.0042 + star.phase);
        const e = Math.min(1, star.energy * shimmer * brightMul);
        drawStar(star.col, star.row, e, star.tint, star.flash);
      });

      for (let i = 0; i < ambient.length; i += 1) {
        const a = ambient[i];
        a.t += dt;
        if (a.t >= a.life) {
          respawn(a);
          continue;
        }
        const env = Math.sin((a.t / a.life) * Math.PI) * AMBIENT_PEAK;
        const shimmer = 0.7 + 0.3 * Math.sin(now * 0.0035 + a.phase);
        drawStar(a.col, a.row, env * shimmer * brightMul, a.tint, 0);
      }

      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(frame);
    };

    const start = () => {
      if (raf) return;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      if (!raf) return;
      cancelAnimationFrame(raf);
      raf = 0;
    };
    /* A hidden tab (or a backgrounded preview pane) freezes rAF; restarting
       with a fresh timestamp avoids a single huge dt on the way back. */
    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    const ro = new ResizeObserver(() => {
      measure();
      refreshRect();
    });
    ro.observe(host);

    window.addEventListener('pointermove', onPointerMove, { passive: true });
    document.addEventListener('pointerout', onPointerOut, { passive: true });
    window.addEventListener('scroll', refreshRect, { passive: true, capture: true });
    window.addEventListener('resize', refreshRect);
    document.addEventListener('visibilitychange', onVisibility);

    start();

    return () => {
      stop();
      ro.disconnect();
      window.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerout', onPointerOut);
      window.removeEventListener('scroll', refreshRect, true);
      window.removeEventListener('resize', refreshRect);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className="absolute inset-0 w-full h-full pointer-events-none"
    />
  );
}
