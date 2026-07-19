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

    // -- Built-in visitor critters (original pixel art, Clawd's grid style) --
    const VISITORS = [
        { name: 'snail', px: 3, palette: { '#': '#A9BE7B', 'O': '#16130E', 's': '#8A795D' }, map: [
            '..####..',
            '.#OO##..',
            '.####s..',
            'ss#ssss.',
            'ssssssss',
        ] },
        { name: 'ghost', px: 3, palette: { '#': '#C8C4B4', 'O': '#16130E' }, map: [
            '.######.',
            '#O####O#',
            '########',
            '########',
            '#.#..#.#',
        ] },
        { name: 'dino', px: 3, palette: { '#': '#7FA8C9', 'O': '#16130E' }, map: [
            '....####',
            '....#O##',
            '#...####',
            '#######.',
            '.#####..',
            '..#..#..',
        ] },
    ];

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

    function visitorElement() {
        // Pool: user PNGs (data URIs) > bundled CC0 pack (DCSS tiles) >
        // the hand-drawn grid critters as a fallback garnish.
        const custom = (lastData && lastData.visitors) || [];
        const pack = (lastData && lastData.visitors_pack) || [];
        const roll = Math.random();
        if (custom.length && roll < 0.4) {
            const img = document.createElement('img');
            img.src = custom[Math.floor(Math.random() * custom.length)];
            img.style.height = '22px';
            return img;
        }
        if (pack.length && roll < 0.85) {
            const img = document.createElement('img');
            img.src = pack[Math.floor(Math.random() * pack.length)];
            img.style.height = '24px';
            return img;
        }
        const critter = VISITORS[Math.floor(Math.random() * VISITORS.length)];
        const el = document.createElement('div');
        const inner = document.createElement('div');
        inner.style.width = `${critter.px}px`;
        inner.style.height = `${critter.px}px`;
        inner.style.boxShadow = critter.map.map((row, y) =>
            [...row].map((ch, x) => critter.palette[ch]
                ? `${x * critter.px}px ${y * critter.px}px 0 0 ${critter.palette[ch]}` : null)
                .filter(Boolean).join(','))
            .filter(Boolean).join(',');
        el.style.width = `${critter.map[0].length * critter.px}px`;
        el.style.height = `${critter.map.length * critter.px}px`;
        el.appendChild(inner);
        return el;
    }

    function spawnVisitor() {
        const layer = document.getElementById('visitorLayer');
        if (!layer) return;
        visitorBusy = true;

        const el = visitorElement();
        el.classList.add('visitor');
        const fromLeft = Math.random() < 0.5;
        const travel = layer.clientWidth + 60;
        const duration = 9000 + Math.random() * 5000;
        el.style.left = fromLeft ? '-40px' : `${layer.clientWidth + 10}px`;
        el.style.transition = `transform ${duration}ms linear`;
        layer.appendChild(el);

        // Sprites notice the passer-by.
        eyeDir = fromLeft ? -1 : 1;
        clawdResting();
        if (Math.random() < 0.4) bubble(els.claudeSprite.parentElement, 'visitor');
        setTimeout(() => { eyeDir = 0; clawdResting(); }, 2600);

        requestAnimationFrame(() => {
            el.style.transform = `translateX(${fromLeft ? travel : -travel}px)`;
        });
        setTimeout(() => { el.remove(); visitorBusy = false; }, duration + 200);
    }

    // ---------------------------------------------------------------------
    // Public hooks
    // ---------------------------------------------------------------------

    return {
        init() {
            if (started) return;
            started = true;
            setInterval(idleTick, 2600);

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
