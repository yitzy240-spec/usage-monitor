let els;
let translations = {};
let lastData = null;
let blinkTimer = null;

// ---------------------------------------------------------------------------
// Pixel mascot - 12x12 maps rendered as box-shadow, no image assets.
// o body / O outline / b dark / w cream
// ---------------------------------------------------------------------------

const PX = 4;
const PALETTE = { o: '#D97757', O: '#8F4A33', b: '#2A1D16', w: '#F0EEE5' };

const FACES = {
    happy: [
        '....OOOO....',
        '..OOooooOO..',
        '.OooooooooO.',
        '.OooooooooO.',
        'OooboooboooO',
        'OooooooooooO',
        'OooboooobooO',
        'OooobbbboooO',
        'OooooooooooO',
        '.OooooooooO.',
        '..OOooooOO..',
        '....O..O....',
    ],
    sweat: [
        '....OOOO....',
        '..OOooooOO..',
        '.OooooooooO.',
        '.OooooooooO.',
        'OooboooboooO',
        'OooooooooooO',
        'OooooooooooO',
        'OoobbbbbbooO',
        'OooooooooooO',
        '.OooooooooO.',
        '..OOooooOO..',
        '....O..O....',
    ],
    panic: [
        '....OOOO....',
        '..OOooooOO..',
        '.OoowowoowO.',
        '.OoobobobwO.',
        'OoowbowobooO',
        'OooooooooooO',
        'OooobbbboooO',
        'OooobbbboooO',
        'OoooobbooooO',
        '.OooooooooO.',
        '..OOooooOO..',
        '...O....O...',
    ],
};

// Eyes-closed variant for the idle blink, derived per mood.
function blinkFrame(map) {
    return map.map((row, y) => (y === 4 ? row.replaceAll('b', 'O').replaceAll('w', 'o') : row));
}

function mapToShadow(map) {
    const shadows = [];
    map.forEach((row, y) => {
        [...row].forEach((ch, x) => {
            if (PALETTE[ch]) {
                shadows.push(`${x * PX}px ${y * PX}px 0 0 ${PALETTE[ch]}`);
            }
        });
    });
    return shadows.join(',');
}

let currentMood = 'happy';

function setMood(mood) {
    currentMood = mood;
    document.body.classList.remove('mood-happy', 'mood-sweat', 'mood-panic');
    document.body.classList.add(`mood-${mood}`);
    els.mascot.style.boxShadow = mapToShadow(FACES[mood]);

    if (blinkTimer) clearInterval(blinkTimer);
    blinkTimer = setInterval(() => {
        els.mascot.style.boxShadow = mapToShadow(blinkFrame(FACES[currentMood]));
        setTimeout(() => {
            els.mascot.style.boxShadow = mapToShadow(FACES[currentMood]);
        }, 140);
    }, 3200);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function severityColor(pct, thresholds) {
    const [lo, hi] = thresholds;
    if (pct >= hi) return 'var(--crit)';
    if (pct >= lo) return 'var(--warn)';
    return 'var(--ok)';
}

function renderRows(container, bars, thresholds) {
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
        fill.style.setProperty('--bar-color', severityColor(bar.fill_pct * 100, thresholds));
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

function renderProvider(sectionEl, rowsEl, planEl, errorEl, provider, thresholds) {
    const visible = !!provider;
    sectionEl.classList.toggle('visible', visible);
    if (!visible) return;

    planEl.textContent = provider.plan || '';
    planEl.style.display = provider.plan ? '' : 'none';
    renderRows(rowsEl, provider.usage || [], thresholds);
    errorEl.textContent = provider.error || '';
    errorEl.style.display = provider.error ? 'block' : 'none';
}

function updateData(data) {
    lastData = data;
    const thresholds = data.thresholds || [70, 90];

    els.worstPct.textContent = `${data.worst_pct}%`;
    els.worstPct.style.color = severityColor(data.worst_pct, thresholds);
    setMood(data.mood || 'happy');

    renderProvider(els.providerClaude, els.claudeRows, els.claudePlan, els.claudeError, data.claude, thresholds);
    renderProvider(els.providerCodex, els.codexRows, els.codexPlan, els.codexError, data.codex, thresholds);

    document.body.classList.toggle('pinned', !!data.pinned);
}

// Replay the spring entrance and bar sweep each time Python shows the window.
function hudShown() {
    document.body.classList.remove('open');
    void document.body.offsetHeight; // restart the entrance animation
    document.body.classList.add('open');

    const fills = document.querySelectorAll('.row-fill');
    fills.forEach((fill) => { fill.style.width = '0%'; });
    requestAnimationFrame(() => requestAnimationFrame(() => {
        fills.forEach((fill) => { fill.style.width = fill.dataset.target; });
    }));
}

function setPinned(pinned) {
    document.body.classList.toggle('pinned', pinned);
    els.pinState.textContent = pinned ? 'pinned' : '';
}

// ---------------------------------------------------------------------------
// Init & interactions
// ---------------------------------------------------------------------------

function init(config) {
    translations = config.t;

    els = {
        mascot: document.getElementById('mascot'),
        tagline: document.getElementById('tagline'),
        worstPct: document.getElementById('worstPct'),
        pinState: document.getElementById('pinState'),
        providerClaude: document.getElementById('providerClaude'),
        claudeRows: document.getElementById('claudeRows'),
        claudePlan: document.getElementById('claudePlan'),
        claudeError: document.getElementById('claudeError'),
        providerCodex: document.getElementById('providerCodex'),
        codexRows: document.getElementById('codexRows'),
        codexPlan: document.getElementById('codexPlan'),
        codexError: document.getElementById('codexError'),
    };

    document.getElementById('claudeName').textContent = translations.claude;
    document.getElementById('codexName').textContent = translations.codex;
    els.tagline.textContent = `${translations.claude} + ${translations.codex}`;

    document.getElementById('closeBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        pywebview.api.close();
    });
    document.getElementById('expandBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        pywebview.api.open_popup();
    });

    // Click anywhere pins the peek; Escape closes.
    document.body.addEventListener('click', () => {
        setPinned(true);
        pywebview.api.pin();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') pywebview.api.close();
    });

    els.pinState.textContent = '';
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
