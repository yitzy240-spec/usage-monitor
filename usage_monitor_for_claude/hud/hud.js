let els;
let translations = {};
let lastData = null;
let blinkTimer = null;

// ---------------------------------------------------------------------------
// Pixel mascot - the canonical Clawd 12x8 grid (recovered from the Claude
// Code logo by ClawdMoji, MIT: github.com/afspies/ClawdMoji), rendered as
// box-shadow so no image assets are needed.  '#' body / 'O' eye.
// ---------------------------------------------------------------------------

const PX = 3;

const CLAWD = [
    '..########..',
    '..#O####O#..',
    '############',
    '############',
    '..########..',
    '..########..',
    '..#.#..#.#..',
    '..#.#..#.#..',
];

const SPRITES = {
    claude: { map: CLAWD, palette: { '#': '#DA7758', 'O': '#16130E' } },
};

// Eyes-closed variant for the idle blink.
function blinkFrame(map) {
    return map.map((row) => row.replaceAll('O', '#'));
}

function mapToShadow(map, palette) {
    const shadows = [];
    map.forEach((row, y) => {
        [...row].forEach((ch, x) => {
            if (palette[ch]) {
                shadows.push(`${x * PX}px ${y * PX}px 0 0 ${palette[ch]}`);
            }
        });
    });
    return shadows.join(',');
}

function startBlink() {
    if (blinkTimer) clearInterval(blinkTimer);
    blinkTimer = setInterval(() => {
        els.claudeSprite.style.boxShadow = mapToShadow(blinkFrame(CLAWD), SPRITES.claude.palette);
        els.codexSprite.src = 'codex-pet-1.png';
        setTimeout(() => {
            els.claudeSprite.style.boxShadow = mapToShadow(CLAWD, SPRITES.claude.palette);
            els.codexSprite.src = 'codex-pet-0.png';
        }, 140);
    }, 3200);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

// Peak-number color: calm cream until the warn threshold, then amber/red.
function severityColor(pct, thresholds) {
    const [lo, hi] = thresholds;
    if (pct >= hi) return 'var(--crit)';
    if (pct >= lo) return 'var(--warn)';
    return 'var(--ink)';
}

// Bars fill in the provider's brand color until the critical threshold,
// then switch to the same true red as the peak numbers.
function barColor(bar, thresholds, brand) {
    const [, hi] = thresholds;
    return bar.fill_pct * 100 >= hi ? 'var(--crit)' : brand;
}

function renderRows(container, bars, thresholds, brand) {
    container.replaceChildren(...bars.map((bar) => {
        const row = document.createElement('div');
        row.className = 'row';

        const label = document.createElement('span');
        label.className = 'row-label';
        label.textContent = bar.label;

        const meta = document.createElement('span');
        meta.className = 'row-meta';
        if (bar.reset_text) {
            const reset = document.createElement('span');
            reset.textContent = bar.reset_text;
            meta.appendChild(reset);
        }
        const pct = document.createElement('span');
        pct.className = 'row-pct';
        pct.textContent = bar.pct_text;
        meta.appendChild(pct);

        const track = document.createElement('div');
        track.className = 'row-bar';
        const fill = document.createElement('div');
        fill.className = 'row-fill';
        fill.style.setProperty('--bar-color', barColor(bar, thresholds, brand));
        fill.dataset.target = `${bar.fill_pct * 100}%`;
        fill.style.width = fill.dataset.target;
        track.appendChild(fill);
        if (bar.marker_rel !== null && bar.marker_rel !== undefined) {
            const marker = document.createElement('div');
            marker.className = 'row-marker';
            marker.style.left = `calc(${bar.marker_rel * 100}% - 1px)`;
            track.appendChild(marker);
        }

        row.append(label, meta, track);
        return row;
    }));
}

function renderProvider(key, provider, thresholds, brand) {
    const sectionEl = els[`provider${key}`];
    const visible = !!provider;
    sectionEl.classList.toggle('visible', visible);
    if (!visible) return;

    const lower = key.toLowerCase();
    sectionEl.classList.remove('mood-happy', 'mood-sweat', 'mood-panic');
    sectionEl.classList.add(`mood-${provider.mood || 'happy'}`);

    const planEl = els[`${lower}Plan`];
    planEl.textContent = provider.plan || '';
    planEl.style.display = provider.plan ? '' : 'none';

    const peakEl = els[`${lower}Peak`];
    if (provider.peak !== null && provider.peak !== undefined) {
        peakEl.textContent = `${provider.peak}%`;
        peakEl.style.color = severityColor(provider.peak, thresholds);
        peakEl.style.display = '';
    } else {
        peakEl.style.display = 'none';
    }

    renderRows(els[`${lower}Rows`], provider.usage || [], thresholds, brand);

    const ctxEl = els[`${lower}Ctx`];
    if (ctxEl) renderContext(ctxEl, provider.sessions || [], thresholds, brand);

    const errorEl = els[`${lower}Error`];
    errorEl.textContent = provider.error || '';
    errorEl.style.display = provider.error ? 'block' : 'none';
}

const SVG_NS = 'http://www.w3.org/2000/svg';

// One circular context gauge: ring fill = window occupancy, % centered.
function contextRing(pct, thresholds, brand) {
    const size = 30, r = 12, c = 2 * Math.PI * r;
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ctx-ring');
    svg.setAttribute('width', size);
    svg.setAttribute('height', size);
    svg.setAttribute('viewBox', `0 0 ${size} ${size}`);

    for (const cls of ['ring-track', 'ring-fill']) {
        const circle = document.createElementNS(SVG_NS, 'circle');
        circle.setAttribute('class', cls);
        circle.setAttribute('cx', size / 2);
        circle.setAttribute('cy', size / 2);
        circle.setAttribute('r', r);
        circle.setAttribute('fill', 'none');
        circle.setAttribute('stroke-width', 3.5);
        if (cls === 'ring-fill') {
            circle.setAttribute('stroke-linecap', 'round');
            circle.setAttribute('stroke-dasharray', c);
            circle.setAttribute('stroke-dashoffset', c * (1 - Math.min(pct, 100) / 100));
            circle.setAttribute('transform', `rotate(-90 ${size / 2} ${size / 2})`);
            circle.style.setProperty('--ring-color', pct >= thresholds[1] ? 'var(--crit)' : brand);
        }
        svg.appendChild(circle);
    }

    const label = document.createElementNS(SVG_NS, 'text');
    label.setAttribute('class', 'ring-pct');
    label.setAttribute('x', size / 2);
    label.setAttribute('y', size / 2);
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('dominant-baseline', 'central');
    label.textContent = `${pct}`;
    svg.appendChild(label);

    return svg;
}

// Context-window fill of active Claude Code sessions, as ring gauges.
function renderContext(container, sessions, thresholds, brand) {
    container.classList.toggle('visible', !!sessions.length);
    if (!sessions.length) {
        container.replaceChildren();
        return;
    }

    const heading = document.createElement('div');
    heading.className = 'ctx-heading';
    heading.textContent = 'ctx';

    container.replaceChildren(heading, ...sessions.map((s) => {
        const item = document.createElement('div');
        item.className = 'ctx-item';
        item.classList.toggle('idle', !!s.idle);
        const tokens = s.tokens >= 1000 ? `${Math.round(s.tokens / 1000)}k` : `${s.tokens}`;
        const ageMin = Math.round((s.age_seconds || 0) / 60);
        item.title = `${s.name} — ${tokens} of ${Math.round(s.limit / 1000)}k (${s.pct}%), last turn ${ageMin}m ago`;

        const text = document.createElement('div');
        text.className = 'ctx-text';
        const name = document.createElement('span');
        name.className = 'ctx-name';
        name.textContent = s.name;
        const tok = document.createElement('span');
        tok.className = 'ctx-tokens';
        tok.textContent = ageMin >= 2 ? `${tokens} · ${ageMin}m` : tokens;
        text.append(name, tok);

        item.append(contextRing(s.pct, thresholds, brand), text);
        return item;
    }));
}

function updateData(data) {
    lastData = data;
    const thresholds = data.thresholds || [70, 90];

    renderProvider('Claude', data.claude, thresholds, 'var(--orange)');
    renderProvider('Codex', data.codex, thresholds, 'var(--codex)');

    setPinMode(!!data.pin_mode);
}

// ---------------------------------------------------------------------------
// Linger: after the hotkey is released (pin mode off), the HUD stays up for
// a grace period before fading. Hovering pauses the countdown so reaching
// for the pin button is never a race; leaving restarts a short one.
// ---------------------------------------------------------------------------

let lingerTimer = null;
let lingerArmed = false;
const LINGER_AFTER_LEAVE_SECONDS = 5;

function cancelLinger(disarm) {
    if (lingerTimer) {
        clearTimeout(lingerTimer);
        lingerTimer = null;
    }
    if (disarm) lingerArmed = false;
}

function beginLinger(seconds) {
    cancelLinger(true);
    lingerArmed = true;
    lingerTimer = setTimeout(() => {
        lingerTimer = null;
        lingerArmed = false;
        pywebview.api.close();
    }, seconds * 1000);
}

// Called by Python right before hiding: play the fade-out.
function hudFadeOut() {
    cancelLinger(true);
    document.body.classList.remove('open');
    document.body.classList.add('closing');
}

// Replay the spring entrance and bar sweep each time Python shows the window.
function hudShown() {
    cancelLinger(true);
    document.body.classList.remove('closing');
    document.body.classList.remove('open');
    void document.body.offsetHeight; // restart the entrance animation
    document.body.classList.add('open');

    const fills = document.querySelectorAll('.row-fill');
    fills.forEach((fill) => { fill.style.width = '0%'; });
    requestAnimationFrame(() => requestAnimationFrame(() => {
        fills.forEach((fill) => { fill.style.width = fill.dataset.target; });
    }));
}

function setPinMode(pinned) {
    document.body.classList.toggle('pinned', pinned);
    els.pinBtn.classList.toggle('pinned', pinned);
    els.pinBtn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    els.pinBtn.title = pinned ? 'Unpin (hide on release)' : 'Pin (stay on screen)';
    els.pinState.textContent = pinned ? 'pinned' : '';
    els.hint.textContent = pinned ? 'esc to close' : 'release to hide · pin to keep';
}

// ---------------------------------------------------------------------------
// Init & interactions
// ---------------------------------------------------------------------------

function init(config) {
    translations = config.t;

    els = {
        tagline: document.getElementById('tagline'),
        pinState: document.getElementById('pinState'),
        hint: document.getElementById('hint'),
        pinBtn: document.getElementById('pinBtn'),
        claudeSprite: document.getElementById('claudeSprite'),
        codexSprite: document.getElementById('codexSprite'),
        providerClaude: document.getElementById('providerClaude'),
        claudeRows: document.getElementById('claudeRows'),
        claudeCtx: document.getElementById('claudeCtx'),
        claudePlan: document.getElementById('claudePlan'),
        claudePeak: document.getElementById('claudePeak'),
        claudeError: document.getElementById('claudeError'),
        providerCodex: document.getElementById('providerCodex'),
        codexRows: document.getElementById('codexRows'),
        codexPlan: document.getElementById('codexPlan'),
        codexPeak: document.getElementById('codexPeak'),
        codexError: document.getElementById('codexError'),
    };

    document.getElementById('claudeName').textContent = translations.claude;
    document.getElementById('codexName').textContent = translations.codex;
    els.tagline.textContent = `${translations.claude} + ${translations.codex}`;

    document.getElementById('closeBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        pywebview.api.close();
    });

    // Pin is a sticky mode: on -> summons stay on screen; off -> the HUD
    // lives only while the hotkey is held.
    els.pinBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const next = !els.pinBtn.classList.contains('pinned');
        if (next) cancelLinger(true); // pinning while lingering keeps it up
        setPinMode(next);
        pywebview.api.set_pin_mode(next).then((applied) => setPinMode(!!applied)).catch(() => setPinMode(!next));
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') pywebview.api.close();
    });

    // Hovering pauses the linger countdown; leaving restarts a short one.
    document.body.addEventListener('mouseenter', () => cancelLinger(false));
    document.body.addEventListener('mouseleave', () => {
        if (lingerArmed) beginLinger(LINGER_AFTER_LEAVE_SECONDS);
    });

    // Drag the HUD anywhere by its background; the spot is remembered.
    let dragging = false;
    document.body.addEventListener('mousedown', (e) => {
        if (e.button !== 0 || e.target.closest('button')) return;
        e.preventDefault();
        pywebview.api.begin_drag().then((started) => { dragging = !!started; }).catch(() => {});
    });
    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        if (e.buttons === 0) {
            dragging = false;
            pywebview.api.end_drag();
            return;
        }
        pywebview.api.drag().catch(() => {});
    });
    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        pywebview.api.end_drag();
    });

    els.claudeSprite.style.boxShadow = mapToShadow(CLAWD, SPRITES.claude.palette);
    startBlink();
    updateData(config.data);

    // Content-driven window height: #card.scrollHeight is the needed height
    // (body is pinned to 100%, so its own scrollHeight never grows).
    const card = document.getElementById('card');
    new ResizeObserver(() => reportHeight(card)).observe(card);
    for (const section of document.querySelectorAll('.provider, .rows')) {
        new ResizeObserver(() => reportHeight(card)).observe(section);
    }
    reportHeight(card);
}

function reportHeight(card) {
    if (window.pywebview?.api?.report_height) {
        pywebview.api.report_height(card.scrollHeight);
    }
}
