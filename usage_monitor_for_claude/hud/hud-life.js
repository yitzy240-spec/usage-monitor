// ---------------------------------------------------------------------------
// hud-life.js - the aliveness engine.
//
// Layers personality on top of hud.js's base rendering: baselines driven by
// state (sleeping / typing / knocked-out), a scheduler of rare idle actions,
// reactions to real events (resets, threshold crossings), cursor awareness,
// speech-bubble quips, and occasional visitor critters.
//
// Shares hud.js's top-level scope (plain scripts): uses els, CLAWD, SPRITES,
// PX, mapToShadow, blinkFrame.
// ---------------------------------------------------------------------------

/* global els, CLAWD, SPRITES, PX, mapToShadow, blinkFrame */

const hudLife = (() => {
    // -- Codex spritesheet map (rows on codex-sheet.webp, cell 28x30 css px) --
    const SHEET = {
        idle: { row: 0, frames: 6, fps: 3 },
        walk: { row: 1, frames: 8, fps: 8 },
        walkLeft: { row: 2, frames: 8, fps: 8 },
        wave: { row: 3, frames: 4, fps: 6 },
        dance: { row: 4, frames: 5, fps: 7 },
        ko: { row: 5, frames: 8, fps: 4 },
        think: { row: 6, frames: 8, fps: 4 },
        typing: { row: 7, frames: 8, fps: 6 },
        happy: { row: 8, frames: 6, fps: 6 },
    };

    // -- Clawd pixel-grid variants (derived from the canonical grid) --
    function shiftEyes(map, dx) {
        return map.map((row) => {
            if (!row.includes('O')) return row;
            const chars = [...row.replaceAll('O', '#')];
            for (let x = 0; x < row.length; x++) {
                if (row[x] === 'O' && chars[x + dx] === '#') chars[x + dx] = 'O';
            }
            return chars.join('');
        });
    }

    const CLAWD_FRAMES = {
        base: CLAWD,
        blink: blinkFrame(CLAWD),
        lookL: shiftEyes(CLAWD, -1),
        lookR: shiftEyes(CLAWD, 1),
        sleep: blinkFrame(CLAWD),
        walk1: [...CLAWD.slice(0, 6), '..#.#...#.#.', '.#.#...#.#..'],
        walk2: [...CLAWD.slice(0, 6), '.#.#...#.#..', '..#.#...#.#.'],
        cheer: [
            '#..######..#',
            '#.#O####O#.#',
            '############',
            '.##########.',
            '..########..',
            '..########..',
            '..#.#..#.#..',
            '..#.#..#.#..',
        ],
    };

    const QUIPS = {
        reset: ['fresh window ✨', 'quota\'s back!', 'we ride again'],
        warn: ['easy on the tokens, boss', 'pace yourself…', 'we\'re burning hot'],
        ko: ['weekly\'s cooked 😵', 'wake me at reset'],
        sleep: ['zzz…'],
        typing: ['shipping…', 'in the zone'],
        visitor: ['who\'s that?', '👀'],
        hello: ['hey!', 'o/'],
        tickle: ['hehe', '!'],
    };

    // -- State --
    let started = false;
    let claudeBaseline = 'idle';   // idle | sleep
    let codexBaseline = 'idle';    // idle | sleep | typing | ko
    let codexAnim = null;          // {row, frames, fps, until, loop}
    let codexTimer = null;
    let clawdOverride = null;      // frame name, transient
    let eyeDir = 0;                // -1 | 0 | 1 from cursor tracking
    let lastQuipAt = 0;
    let visitorBusy = false;

    // ---------------------------------------------------------------------
    // Low-level frame drivers
    // ---------------------------------------------------------------------

    function setCodexCell(row, col) {
        els.codexSprite.style.backgroundPosition = `-${col * 28}px -${row * 30}px`;
    }

    function playCodex(name, { loop = false, durationMs = null } = {}) {
        const anim = SHEET[name];
        if (!anim) return;
        if (codexTimer) clearInterval(codexTimer);
        let frame = 0;
        const until = durationMs ? Date.now() + durationMs : null;
        setCodexCell(anim.row, 0);
        codexTimer = setInterval(() => {
            frame = (frame + 1) % anim.frames;
            if (frame === 0 && !loop && (!until || Date.now() >= until)) {
                clearInterval(codexTimer);
                codexTimer = null;
                applyCodexBaseline();
                return;
            }
            if (until && Date.now() >= until && frame === 0) {
                clearInterval(codexTimer);
                codexTimer = null;
                applyCodexBaseline();
                return;
            }
            setCodexCell(anim.row, frame);
        }, 1000 / anim.fps);
    }

    function drawClawd(frameName) {
        const frame = CLAWD_FRAMES[frameName] || CLAWD_FRAMES.base;
        els.claudeSprite.style.boxShadow = mapToShadow(frame, SPRITES.claude.palette);
    }

    function clawdResting() {
        if (clawdOverride) return;
        if (claudeBaseline === 'sleep') drawClawd('sleep');
        else drawClawd(eyeDir < 0 ? 'lookL' : eyeDir > 0 ? 'lookR' : 'base');
    }

    function applyCodexBaseline() {
        if (codexTimer) return; // an animation owns the sprite right now
        if (codexBaseline === 'ko') playCodex('ko', { loop: true });
        else if (codexBaseline === 'typing') playCodex('typing', { loop: true });
        else if (codexBaseline === 'sleep') setCodexCell(0, 1); // eyes closed
        else setCodexCell(0, 0);
    }

    // ---------------------------------------------------------------------
    // Z's, bubbles, confetti
    // ---------------------------------------------------------------------

    function setZzz(box, on) {
        let z = box.querySelector('.zzz');
        if (on && !z) {
            z = document.createElement('span');
            z.className = 'zzz';
            z.textContent = 'z';
            box.appendChild(z);
        } else if (!on && z) {
            z.remove();
        }
    }

    function bubble(box, category) {
        const now = Date.now();
        if (now - lastQuipAt < 20000) return; // quips stay rare
        lastQuipAt = now;
        const lines = QUIPS[category] || [];
        if (!lines.length) return;
        const el = document.createElement('div');
        el.className = 'bubble';
        el.textContent = lines[Math.floor(Math.random() * lines.length)];
        box.appendChild(el);
        setTimeout(() => el.remove(), 2600);
    }

    function confetti(box) {
        const colors = ['#DA7758', '#A78BFA', '#A9BE7B', '#E5B567', '#F0EEE5'];
        for (let i = 0; i < 14; i++) {
            const p = document.createElement('div');
            p.className = 'confetti';
            p.style.background = colors[i % colors.length];
            p.style.left = `${4 + Math.random() * 36}px`;
            p.style.top = '-4px';
            p.style.setProperty('--cx', `${(Math.random() - 0.5) * 40}px`);
            p.style.animationDelay = `${Math.random() * 250}ms`;
            box.appendChild(p);
            setTimeout(() => p.remove(), 1600);
        }
    }

    function pulse(el, cls, ms) {
        el.classList.remove(cls);
        void el.offsetWidth;
        el.classList.add(cls);
        setTimeout(() => el.classList.remove(cls), ms);
    }

    // ---------------------------------------------------------------------
    // Baselines from data
    // ---------------------------------------------------------------------

    function refreshBaselines(data) {
        const hour = new Date().getHours();
        const night = hour >= 23 || hour < 7;

        const claudePeak = data.claude?.peak ?? 0;
        const sessions = data.claude?.sessions || [];
        const activeSessions = sessions.some((s) => !s.idle);
        claudeBaseline = (claudePeak < 3 && !activeSessions && night) ? 'sleep' : 'idle';
        setZzz(els.claudeSprite.parentElement, claudeBaseline === 'sleep');
        clawdResting();

        const codex = data.codex;
        if (!codex) return;
        const codexPeak = codex.peak ?? 0;
        const codexActive = (codex.usage || []).some((b) => b.key === 'five_hour' && b.fill_pct > 0.01);
        let next = 'idle';
        if (codexPeak >= 100) next = 'ko';
        else if (codexActive) next = 'typing';
        else if (codexPeak < 3 && night) next = 'sleep';
        if (next !== codexBaseline) {
            codexBaseline = next;
            if (codexTimer) { clearInterval(codexTimer); codexTimer = null; }
            applyCodexBaseline();
            if (next === 'ko') bubble(els.codexSprite.parentElement, 'ko');
            if (next === 'typing') bubble(els.codexSprite.parentElement, 'typing');
        }
        setZzz(els.codexSprite.parentElement, codexBaseline === 'sleep');
    }

    // ---------------------------------------------------------------------
    // Event reactions (data diffs)
    // ---------------------------------------------------------------------

    function detectEvents(data, prev) {
        if (!prev) return;
        for (const key of ['claude', 'codex']) {
            const now = data[key]?.peak;
            const was = prev[key]?.peak;
            if (now == null || was == null) continue;
            const box = els[`${key}Sprite`].parentElement;

            if (was - now > 25) { // a window reset
                confetti(box.parentElement.parentElement);
                if (key === 'codex') playCodex('dance', { durationMs: 2600 });
                else {
                    clawdOverride = 'cheer';
                    drawClawd('cheer');
                    pulse(els.claudeSprite, 'hop', 600);
                    setTimeout(() => { clawdOverride = null; clawdResting(); }, 2600);
                }
                bubble(box, 'reset');
            } else if (was < 90 && now >= 90) { // crossed critical
                pulse(els[`${key}Sprite`], 'hop', 600);
                bubble(box, 'warn');
            }
        }
    }

    // ---------------------------------------------------------------------
    // Idle scheduler - rare random life
    // ---------------------------------------------------------------------

    function idleTick() {
        if (document.hidden) return;
        const roll = Math.random();

        // Blinks are frequent; everything else is rare.
        if (roll < 0.55) {
            if (claudeBaseline === 'idle' && !clawdOverride) {
                drawClawd('blink');
                setTimeout(clawdResting, 140);
            }
            if (codexBaseline === 'idle' && !codexTimer) {
                setCodexCell(0, 1);
                setTimeout(applyCodexBaseline, 140);
            }
        } else if (roll < 0.70 && claudeBaseline === 'idle' && !clawdOverride) {
            // Clawd glances around (or at his buddy).
            const dir = Math.random() < 0.5 ? 'lookL' : 'lookR';
            clawdOverride = dir;
            drawClawd(dir);
            setTimeout(() => { clawdOverride = null; clawdResting(); }, 1300);
        } else if (roll < 0.82 && codexBaseline === 'idle' && !codexTimer) {
            playCodex(Math.random() < 0.5 ? 'walk' : 'think', { durationMs: 1600 });
        } else if (roll < 0.86 && claudeBaseline === 'idle' && !clawdOverride) {
            // Clawd takes a couple of steps in place.
            clawdOverride = 'walk1';
            let steps = 0;
            const walker = setInterval(() => {
                drawClawd(steps % 2 ? 'walk1' : 'walk2');
                if (++steps >= 6) {
                    clearInterval(walker);
                    clawdOverride = null;
                    clawdResting();
                }
            }, 180);
        } else if (roll < 0.88 && !visitorBusy) {
            spawnVisitor();
        }
    }

    // ---------------------------------------------------------------------
    // Visitors
    // ---------------------------------------------------------------------

    function gridShadow(rows, palette, px) {
        return rows.map((row, y) =>
            [...row].map((ch, x) => palette[ch]
                ? `${x * px}px ${y * px}px 0 0 ${palette[ch]}` : null)
                .filter(Boolean).join(','))
            .filter(Boolean).join(',');
    }

    function gridElement(critter) {
        const el = document.createElement('div');
        // Density-aware: dense 40+-row grids render at 1px/cell (~40px tall,
        // Codex-sprite territory), chunky 8-row grids at 3px.
        const px = critter.px || Math.max(1, Math.round(30 / critter.rows.length));
        const inner = document.createElement('div');
        inner.style.width = `${px}px`;
        inner.style.height = `${px}px`;
        inner.style.boxShadow = gridShadow(critter.rows, critter.palette, px);
        el.style.width = `${critter.rows[0].length * px}px`;
        el.style.height = `${critter.rows.length * px}px`;
        el.appendChild(inner);
        // Frame-swap hook for animated (Sprite Builder) critters.
        el._setFrame = (name) => {
            const rows = (name && critter.frames && critter.frames[name]) || critter.rows;
            inner.style.boxShadow = gridShadow(rows, critter.palette, px);
        };
        el._hasFrame = (name) => !!(critter.frames && critter.frames[name]);
        return el;
    }

    // -- Petdex pets: full 8x9 spritesheet visitors with real animations --

    let PETS = [];
    let petsRev = null;

    function syncPets(data) {
        const rev = data && data.pets_rev;
        if (!rev || rev === petsRev) return;
        try {
            pywebview.api.get_pets().then((pets) => {
                PETS = pets || [];
                petsRev = rev;
            }).catch(() => {});
        } catch (e) { /* bridge not ready - next payload retries */ }
    }

    // Row layout of the official Codex pet sheet (the states a roamer uses).
    const PET_ROWS = { idle: 0, walk: 1, walkLeft: 2, wave: 3, jump: 4 };
    const PET_W = 26, PET_H = 28; // one 192x208 cell at HUD scale

    function petElement(pet) {
        const el = document.createElement('div');
        el.style.width = `${PET_W}px`;
        el.style.height = `${PET_H}px`;
        el.style.backgroundImage = `url(${pet.sheet})`;
        el.style.backgroundSize = `${PET_W * 8}px ${PET_H * (pet.sheetRows || 9)}px`;
        el.style.backgroundRepeat = 'no-repeat';
        el.style.imageRendering = 'auto'; // hi-res art downscaled, not pixel art
        const frames = pet.rowFrames || [6, 8, 8, 4, 5, 8, 8, 8, 6];
        let anim = 'idle', frame = 0, oneshotUntil = 0;
        const paint = () => {
            el.style.backgroundPosition = `${-frame * PET_W}px ${-(PET_ROWS[anim] || 0) * PET_H}px`;
        };
        el._setAnim = (name, holdMs) => {
            if (!(name in PET_ROWS)) name = 'idle';
            if (holdMs) oneshotUntil = performance.now() + holdMs;
            else if (performance.now() < oneshotUntil) return; // one-shot (wave) plays out
            if (name === anim) return;
            anim = name;
            frame = 0;
            paint();
        };
        // Generic visitor hooks: pets greet with their real wave row.
        el._hasFrame = (name) => name === 'wave';
        el._setFrame = (name) => { if (name === 'wave') el._setAnim('wave', 1100); };
        el._noFlip = true; // native left-facing rows - never mirror
        let connected = false;
        const tick = setInterval(() => {
            if (el.isConnected) connected = true;
            else if (connected) { clearInterval(tick); return; }
            if (anim === 'wave' && performance.now() >= oneshotUntil) { anim = 'idle'; frame = 0; }
            frame = (frame + 1) % (frames[PET_ROWS[anim] || 0] || 1);
            paint();
        }, 140);
        paint();
        return el;
    }

    // True for critters that hover instead of obeying gravity.
    const FLOATY = /bat|butterfly|jelly|eyeball|orb|ghost/;

    function pickVisitor() {
        // Petdex/Codex pets ARE the visitor cast; user-dropped PNGs and
        // Sprite Builder critters still get walk-on parts. Nothing
        // installed -> nobody visits (adopt pets in Settings).
        const custom = (lastData && lastData.visitors) || [];
        const grids = (lastData && lastData.visitor_grids) || [];
        const pool = [];
        for (const pet of PETS) {
            pool.push({ kind: 'pet', pet, floats: false }, { kind: 'pet', pet, floats: false });
        }
        for (const src of custom) pool.push({ kind: 'img', src, floats: false });
        for (const g of grids) pool.push({ kind: 'grid', grid: g, floats: FLOATY.test(g.name || '') });
        if (!pool.length) return null;
        return pool[Math.floor(Math.random() * pool.length)];
    }

    // -- Platform physics: real element rects become walkable surfaces --

    function collectPlatforms(layer) {
        const base = layer.getBoundingClientRect();
        const card = document.getElementById('card').getBoundingClientRect();
        const platforms = [];
        const add = (rect, inset = 2) => {
            if (rect.width < 30) return;
            platforms.push({
                left: rect.left - base.left + inset,
                right: rect.right - base.left - inset,
                y: rect.top - base.top,
            });
        };
        for (const el of document.querySelectorAll('.row-bar, .provider-head, .ctx.visible')) {
            add(el.getBoundingClientRect());
        }
        // The floor: the card's bottom edge (inside the padding).
        platforms.push({
            left: card.left - base.left + 6,
            right: card.right - base.left - 6,
            y: card.bottom - base.top - 5,
        });
        return platforms.sort((a, b) => a.y - b.y);
    }

    function platformBelow(platforms, x, y) {
        let best = null;
        for (const p of platforms) {
            if (p.y > y + 1 && x >= p.left - 4 && x <= p.right + 4) {
                if (!best || p.y < best.y) best = p;
            }
        }
        return best;
    }

    function spawnVisitor() {
        if (lastData && lastData.visitors_enabled === false) return;
        const layerFront = document.getElementById('visitorLayer');
        const layerBack = document.getElementById('visitorLayerBack');
        const cardEl = document.getElementById('card');
        if (!layerFront || !layerBack || layerFront.clientWidth < 100) return;
        const pick = pickVisitor();
        if (!pick) return;
        visitorBusy = true;
        const layer = layerFront; // shared coordinate space (both span the window)

        const content = pick.kind === 'img'
            ? Object.assign(document.createElement('img'), { src: pick.src })
            : pick.kind === 'pet' ? petElement(pick.pet)
            : gridElement(pick.grid);
        if (pick.kind === 'img') content.style.height = '24px';
        const el = document.createElement('div');
        el.className = 'visitor';
        el.appendChild(content);
        layerBack.appendChild(el); // born BEHIND the card

        // Measured, not assumed: real sizes are what make feet land ON
        // surfaces instead of sinking through them.
        let w = 24, h = 24;
        function measure() {
            w = el.offsetWidth || w;
            h = el.offsetHeight || h;
        }

        const cardRect = () => {
            const b = layer.getBoundingClientRect();
            const c = cardEl.getBoundingClientRect();
            return { top: c.top - b.top, bottom: c.bottom - b.top, left: c.left - b.left, right: c.right - b.left };
        };

        const card = cardRect();
        // With a transparent apron the critter peeks from behind the rim;
        // without one (card == window) it drops straight into view instead
        // of lingering half-clipped at the top edge.
        const hasApron = card.top > 20;
        let x = card.left + 30 + Math.random() * (card.right - card.left - 60);
        let y = hasApron ? card.top + 6 : -26;
        let vx = (Math.random() < 0.5 ? 1 : -1) * (10 + Math.random() * 12);
        let vy = 0;
        let mode = hasApron ? 'peek' : 'drop';  // peek | climb | drop | walk | pause | greet | leave
        let modeUntil = performance.now() + 900 + Math.random() * 900;
        if (!hasApron) layerFront.appendChild(el);
        let platforms = collectPlatforms(layer);
        const leaveAt = Date.now() + 15000 + Math.random() * 20000;
        let lastT = performance.now();
        let lastNotice = 0;

        // Visitors pay a social call: pick a resident to greet on the way.
        let greetKey = Math.random() < 0.5 ? 'claude' : 'codex';
        function greetTarget() {
            if (!greetKey) return null;
            const box = els[`${greetKey}Sprite`].parentElement.getBoundingClientRect();
            const base = layer.getBoundingClientRect();
            return { x: box.right - base.left + 14, y: box.bottom - base.top };
        }

        function doGreet() {
            const box = els[`${greetKey}Sprite`].parentElement;
            pulse(el.firstChild || el, 'hop', 550);
            // Animated custom sprites wave hello with their own frame.
            if (content._hasFrame && content._hasFrame('wave')) {
                content._setFrame('wave');
                setTimeout(() => content._setFrame(null), 1100);
            }
            if (greetKey === 'codex') {
                if (!codexTimer || codexBaseline !== 'ko') playCodex('wave', { durationMs: 1200 });
            } else if (claudeBaseline === 'idle') {
                clawdOverride = 'cheer';
                drawClawd('cheer');
                pulse(els.claudeSprite, 'hop', 550);
                setTimeout(() => { clawdOverride = null; clawdResting(); }, 1400);
            }
            if (Math.random() < 0.6) bubble(box, 'hello');
            if (Math.random() < 0.5) floatBubble(layer, x, y, '!');
            greetKey = null; // one social call per visit
        }

        function render() {
            const flip = content._noFlip ? 1 : (vx < 0 ? -1 : 1);
            el.style.transform = `translate(${x - w / 2}px, ${y}px) scaleX(${flip})`;
        }

        // Sheet-animated pets act out what the physics is doing to them.
        function syncAnim() {
            if (!content._setAnim) return;
            if (mode === 'walk' || (mode === 'leave' && !pick.floats)) content._setAnim(vx < 0 ? 'walkLeft' : 'walk');
            else if (mode === 'drop' || mode === 'climb') content._setAnim('jump');
            else content._setAnim('idle'); // peek / pause / greet
        }

        function residentsNotice() {
            const now = Date.now();
            if (now - lastNotice < 1500) return;
            lastNotice = now;
            const rect = els.claudeSprite.parentElement.getBoundingClientRect();
            const base = layer.getBoundingClientRect();
            eyeDir = x < (rect.left - base.left) ? -1 : 1;
            clawdResting();
        }

        function step(t) {
            const dt = Math.min((t - lastT) / 1000, 0.05);
            lastT = t;
            measure();
            const floor = platformBelow(platforms, x, y + h - 2);

            if (mode === 'peek') {
                // Rising from behind the rim: only the head clears the card
                // top - the card itself occludes the body (back layer).
                const target = card.top - h * 0.55;
                y = Math.max(target, y - 26 * dt);
                y += Math.sin(t / 160) * 0.15; // curious little bob
                if (t >= modeUntil) {
                    mode = 'climb';
                    modeUntil = t + 450;
                }
            } else if (mode === 'climb') {
                // Hop up onto the rim, crossing to the FRONT layer mid-hop.
                if (el.parentElement !== layerFront) layerFront.appendChild(el);
                const target = card.top - h + 2;
                y += (target - y) * Math.min(1, dt * 9);
                if (t >= modeUntil) {
                    mode = 'drop';
                    vy = 0;
                    platforms = collectPlatforms(layer);
                }
            } else if (pick.floats) {
                // Floaters drift on a sine path between the furniture.
                y += Math.sin(t / 500) * 0.3 + (mode === 'leave' ? -34 * dt : 8 * dt * (y < card.top + 24 ? 1 : 0.15));
                x += vx * dt;
                if (x < card.left + 12 || x > card.right - 12) vx = -vx;
            } else if (mode === 'drop') {
                vy += 260 * dt; // gravity
                y += vy * dt;
                const target = floor ? floor.y - h : layer.clientHeight;
                if (y >= target) {
                    y = target;
                    vy = 0;
                    mode = 'walk';
                    content.classList.add('land-squash');
                    setTimeout(() => content.classList.remove('land-squash'), 240);
                }
            } else if (mode === 'greet') {
                if (t >= modeUntil) mode = 'walk';
            } else if (mode === 'walk') {
                // Steer toward the resident being visited when one is set.
                const target = greetTarget();
                if (target) {
                    if (Math.abs(y + h - target.y) < 8 && Math.abs(x - target.x) < 16) {
                        mode = 'greet';
                        modeUntil = t + 2400;
                        doGreet();
                    } else if (Math.abs(y + h - target.y) < 8) {
                        vx = Math.sign(target.x - x) * Math.abs(vx || 12);
                    } else if (y + h > target.y + 10) {
                        greetKey = null; // fell past them; maybe next time
                    }
                }
                x += vx * dt;
                const on = platforms.find((p) => Math.abs(p.y - h - y) < 3 && x >= p.left - 6 && x <= p.right + 6);
                if (!on) {
                    mode = 'drop'; // walked off an edge - fall to the next thing
                    platforms = collectPlatforms(layer);
                } else if (x <= on.left || x >= on.right) {
                    // Edge of the furniture: peer over it, then turn or hop off.
                    // Hopping off the FLOOR means leaving - only near the end
                    // of the visit; furniture edges just drop to the next shelf.
                    const isFloor = on.y >= layer.clientHeight - 8;
                    const mayExit = Date.now() > leaveAt - 6000;
                    if ((!isFloor || mayExit) && Math.random() < 0.4) {
                        mode = 'drop';
                    } else {
                        vx = -vx;
                        mode = 'pause';
                        modeUntil = t + 700 + Math.random() * 1800;
                        if (Math.random() < 0.12) floatBubble(layer, x, y, '?');
                    }
                } else if (Math.random() < dt * 0.25) {
                    mode = 'pause'; // stops to sniff at the pixels
                    modeUntil = t + 600 + Math.random() * 1600;
                }
            } else if (mode === 'pause') {
                if (t >= modeUntil) mode = 'walk';
            }

            if (mode !== 'leave' && Date.now() > leaveAt) {
                mode = 'leave';
                if (!pick.floats) vy = -90; // little hop before falling off
            }
            if (mode === 'leave' && !pick.floats) {
                vy += 300 * dt;
                y += vy * dt;
                x += vx * dt;
            }

            syncAnim();
            residentsNotice();
            render();

            const gone = pick.floats ? (mode === 'leave' && y < -h - 6) : y > layer.clientHeight + 8;
            if (gone || !document.body.contains(el)) {
                el.remove();
                visitorBusy = false;
                eyeDir = 0;
                clawdResting();
                return;
            }
            requestAnimationFrame(step);
        }

        if (Math.random() < 0.35) bubble(els.claudeSprite.parentElement, 'visitor');
        // Animated custom sprites blink on their own heartbeat.
        let visitorBlink = null;
        if (content._hasFrame && content._hasFrame('blink')) {
            visitorBlink = setInterval(() => {
                content._setFrame('blink');
                setTimeout(() => content._setFrame(null), 160);
            }, 2600 + Math.random() * 1400);
            const clear = new MutationObserver(() => {
                if (!document.body.contains(el)) {
                    clearInterval(visitorBlink);
                    clear.disconnect();
                }
            });
            clear.observe(document.body, { childList: true, subtree: true });
        }
        render();
        requestAnimationFrame(step);
    }

    function floatBubble(layer, x, y, text) {
        const b = document.createElement('div');
        b.className = 'float-bubble';
        b.textContent = text;
        b.style.left = `${x + 8}px`;
        b.style.top = `${y - 14}px`;
        layer.appendChild(b);
        setTimeout(() => b.remove(), 1800);
    }

    // ---------------------------------------------------------------------
    // Public hooks
    // ---------------------------------------------------------------------

    return {
        init() {
            if (started) return;
            started = true;
            setInterval(idleTick, 2600);

            // Easter egg (and demo hook): double-click the tagline to
            // summon a visitor immediately.
            els.tagline.addEventListener('dblclick', (e) => {
                e.stopPropagation();
                if (!visitorBusy) spawnVisitor();
            });

            for (const key of ['claude', 'codex']) {
                const box = els[`${key}Sprite`].parentElement;
                box.addEventListener('mouseenter', () => {
                    pulse(els[`${key}Sprite`], 'tickle', 450);
                    if (Math.random() < 0.25) bubble(box, 'tickle');
                });
                box.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (key === 'codex') playCodex('happy', { durationMs: 1400 });
                    else pulse(els.claudeSprite, 'spin', 700);
                });
            }
            applyCodexBaseline();
        },

        onData(data, prev) {
            refreshBaselines(data);
            detectEvents(data, prev);
            syncPets(data);
        },

        onShown() {
            // A little hello: Codex waves, Clawd hops.
            if (codexBaseline === 'idle') playCodex('wave', { durationMs: 900 });
            if (claudeBaseline === 'idle') pulse(els.claudeSprite, 'hop', 550);
            if (Math.random() < 0.15) bubble(els.codexSprite.parentElement, 'hello');
        },

        onCursor(e) {
            if (claudeBaseline !== 'idle' || clawdOverride) return;
            const rect = els.claudeSprite.parentElement.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const next = e.clientX < cx - 25 ? -1 : e.clientX > cx + 25 ? 1 : 0;
            if (next !== eyeDir) {
                eyeDir = next;
                clawdResting();
            }
        },

        onDragMove(dx) {
            if (!dx) return;
            document.body.classList.toggle('lean-left', dx < 0);
            document.body.classList.toggle('lean-right', dx > 0);
        },

        onDragEnd() {
            document.body.classList.remove('lean-left', 'lean-right');
            pulse(els.claudeSprite, 'hop', 500);
            pulse(els.codexSprite, 'hop', 500);
        },
    };
})();

window.hudLife = hudLife;
