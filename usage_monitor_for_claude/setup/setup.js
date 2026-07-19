let state = null;
let dirty = false;

const $ = (id) => document.getElementById(id);

function showStep(id) {
    for (const step of document.querySelectorAll('.step')) {
        step.classList.toggle('active', step.id === id);
    }
}

// True from "start sign-in" until success/cancel. While set, renderAccounts
// must not touch the flow's UI - the 5s status poll was closing the
// paste-code field mid-flow.
let oauthInProgress = false;

function renderAccounts(accounts) {
    for (const [key, el] of [['claude', $('accClaude')], ['codex', $('accCodex')]]) {
        const ok = !!accounts[key];
        el.classList.toggle('ok', ok);
        el.classList.toggle('missing', !ok);
        el.querySelector('.acc-status').textContent = ok ? 'signed in' : 'not signed in';
    }
    const appLogin = !!accounts.claude_app_login;
    $('accClaude').querySelector('.acc-status').textContent =
        accounts.claude ? (appLogin ? 'signed in (app login)' : 'signed in') : 'not signed in';
    $('signOutRow').classList.toggle('app-login', appLogin);
    $('claudeAltLogin').style.display =
        (accounts.claude && !appLogin && !oauthInProgress) ? '' : 'none';
}

function collectSettings() {
    return {
        hud_hotkey: $('hotkeyInput').value.trim(),
        hud_linger: Number($('lingerInput').value),
        hud_sessions: $('sessionsInput').checked,
        hud_visitors: $('visitorsInput').checked,
        codex_enabled: $('codexInput').checked,
    };
}

function settingsChanged() {
    const now = collectSettings();
    const was = state.settings;
    return now.hud_hotkey !== was.hud_hotkey
        || now.hud_linger !== was.hud_linger
        || now.hud_sessions !== was.hud_sessions
        || now.hud_visitors !== was.hud_visitors
        || now.codex_enabled !== was.codex_enabled;
}

async function validateHotkey() {
    const input = $('hotkeyInput');
    const ok = await pywebview.api.check_hotkey(input.value.trim());
    input.classList.toggle('invalid', !ok);
    $('hotkeyError').classList.toggle('visible', !ok);
    return ok;
}

// ---------------------------------------------------------------------------
// Hotkey recorder: click the box, press the chord - no typing key names.
// ---------------------------------------------------------------------------

let lastValidHotkey = '';

const NAMED_CODES = {
    Space: 'space', Tab: 'tab', Enter: 'enter', Backspace: 'backspace',
    Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown',
    Insert: 'insert', Delete: 'delete',
    ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
    Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']',
    Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/', Backslash: '\\',
};

function mainKeyToken(e) {
    const c = e.code;
    if (/^Key[A-Z]$/.test(c)) return c[3].toLowerCase();
    if (/^Digit[0-9]$/.test(c)) return c[5];
    if (/^F([1-9]|1[0-9]|2[0-4])$/.test(c)) return c.toLowerCase();
    return NAMED_CODES[c] || null;
}

function setupHotkeyRecorder() {
    const input = $('hotkeyInput');
    input.readOnly = true;

    input.addEventListener('focus', () => {
        input.dataset.prev = input.value;
        input.value = '';
        input.placeholder = 'press keys…';
    });

    input.addEventListener('keydown', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.key === 'Escape') {
            input.value = input.dataset.prev || lastValidHotkey;
            input.blur();
            return;
        }

        const mods = [];
        if (e.ctrlKey) mods.push('ctrl');
        if (e.altKey) mods.push('alt');
        if (e.shiftKey) mods.push('shift');
        if (e.metaKey) mods.push('win');

        const main = mainKeyToken(e);
        if (!main) {
            // Modifiers held, chord not complete yet.
            input.value = mods.length ? `${mods.join('+')}+…` : '';
            return;
        }

        input.value = [...mods, main].join('+');
        if (await validateHotkey()) {
            lastValidHotkey = input.value;
            input.blur();
        }
    });

    input.addEventListener('blur', async () => {
        if (!input.value || input.value.endsWith('…') || !(await pywebview.api.check_hotkey(input.value))) {
            input.value = lastValidHotkey || input.dataset.prev || '';
        }
        input.classList.remove('invalid');
        $('hotkeyError').classList.remove('visible');
    });
}

async function save() {
    if (!(await validateHotkey())) return false;
    dirty = settingsChanged();
    const result = await pywebview.api.save_settings(collectSettings());
    $('savePath').textContent = result.ok ? `Saved to ${result.path}` : `Could not save: ${result.error}`;
    return result.ok;
}

async function init() {
    state = await pywebview.api.get_state();
    document.body.classList.add(`mode-${state.mode}`);
    $('subtitle').textContent = state.mode === 'onboarding' ? 'setup' : 'settings';

    renderAccounts(state.accounts);

    $('hotkeyInput').value = state.settings.hud_hotkey;
    lastValidHotkey = state.settings.hud_hotkey;
    setupHotkeyRecorder();
    $('lingerInput').value = state.settings.hud_linger;
    $('lingerVal').textContent = `${state.settings.hud_linger}s`;
    $('sessionsInput').checked = state.settings.hud_sessions;
    $('visitorsInput').checked = state.settings.hud_visitors !== false;
    $('codexInput').checked = state.settings.codex_enabled;
    $('autostartField').style.display = state.frozen ? '' : 'none';
    $('autostartInput').checked = state.autostart;

    showStep(state.mode === 'onboarding' ? 'stepWelcome' : 'stepAccounts');
    if (state.mode === 'settings') {
        // Settings mode shows accounts + prefs + builder + pets as one page.
        $('stepPrefs').classList.add('active');
        $('stepBuilder').classList.add('active');
        $('stepPetdex').classList.add('active');
    }

    // Wiring
    for (const btn of document.querySelectorAll('[data-next]')) {
        btn.addEventListener('click', () => showStep(btn.dataset.next));
    }
    for (const btn of document.querySelectorAll('[data-login]')) {
        btn.addEventListener('click', () => {
            pywebview.api.open_login(btn.dataset.login);
            setTimeout(recheck, 4000);
        });
    }
    $('recheckBtn').addEventListener('click', recheck);

    // App login (OAuth) for claude.ai users without the CLI. One explicit
    // state machine: start -> code row visible until Confirm succeeds or
    // Cancel - the periodic recheck never touches it.
    async function startAppLogin() {
        if (oauthInProgress) return;
        oauthInProgress = true;
        $('claudeAltLogin').style.display = 'none';
        const err = $('oauthError');
        err.textContent = '';
        err.classList.remove('visible');
        const started = await pywebview.api.claude_login_start().catch(() => false);
        if (started) {
            $('oauthCodeRow').classList.add('visible');
            $('oauthCode').focus();
        } else {
            oauthInProgress = false;
            err.textContent = 'Could not open the browser.';
            err.classList.add('visible');
            recheck();
        }
    }

    function endAppLogin() {
        oauthInProgress = false;
        $('oauthCodeRow').classList.remove('visible');
        $('oauthCode').value = '';
        $('oauthError').classList.remove('visible');
        recheck();
    }

    $('claudeOauthBtn').addEventListener('click', startAppLogin);
    $('claudeAltLogin').addEventListener('click', startAppLogin);
    $('oauthCancel').addEventListener('click', endAppLogin);
    $('oauthConfirm').addEventListener('click', async () => {
        const result = await pywebview.api.claude_login_finish($('oauthCode').value)
            .catch(() => ({ ok: false, error: 'bridge error' }));
        if (result.ok) {
            endAppLogin();
        } else {
            const err = $('oauthError');
            err.classList.add('visible');
            err.textContent = result.error || 'sign-in failed';
        }
    });
    $('claudeSignOutBtn').addEventListener('click', async () => {
        renderAccounts(await pywebview.api.claude_sign_out().catch(() => ({})));
    });
    $('lingerInput').addEventListener('input', () => {
        $('lingerVal').textContent = `${$('lingerInput').value}s`;
    });
    $('autostartInput').addEventListener('change', async () => {
        $('autostartInput').checked = await pywebview.api.set_autostart($('autostartInput').checked);
    });

    $('wizardSaveBtn').addEventListener('click', async () => {
        if (!(await save())) return;
        $('doneHotkey').textContent = collectSettings().hud_hotkey;
        $('restartNote').style.display = dirty ? '' : 'none';
        $('restartBtn').style.display = dirty ? '' : 'none';
        showStep('stepDone');
    });
    $('saveBtn').addEventListener('click', async () => {
        if (!(await save())) return;
        if (settingsChangedFromBoot()) {
            $('savePath').textContent += ' — restarting to apply…';
            setTimeout(() => pywebview.api.restart_app(), 600);
        }
    });
    $('restartBtn').addEventListener('click', () => pywebview.api.restart_app());
    $('laterBtn').addEventListener('click', () => pywebview.api.finish());

    // Sprite Builder
    let pendingGrid = null;
    let previewTimer = null;
    function renderGridPreview(grid) {
        const px = Math.max(2, Math.round(130 / grid.rows.length));
        const shadow = (rows) => rows.map((row, y) =>
            [...row].map((ch, x) => grid.palette[ch]
                ? `${x * px}px ${y * px}px 0 0 ${grid.palette[ch]}` : null)
                .filter(Boolean).join(','))
            .filter(Boolean).join(',');
        const inner = document.createElement('div');
        inner.style.width = `${px}px`;
        inner.style.height = `${px}px`;
        inner.style.boxShadow = shadow(grid.rows);
        const frame = document.createElement('div');
        frame.style.width = `${grid.rows[0].length * px}px`;
        frame.style.height = `${grid.rows.length * px}px`;
        frame.appendChild(inner);
        $('spritePreview').replaceChildren(frame);

        // Cycle animation frames in the preview so life is visible upfront.
        if (previewTimer) clearInterval(previewTimer);
        const frames = grid.frames || {};
        const cycle = ['blink', 'wave'].filter((f) => frames[f]);
        if (cycle.length) {
            let i = 0;
            previewTimer = setInterval(() => {
                const name = cycle[i++ % cycle.length];
                inner.style.boxShadow = shadow(frames[name]);
                setTimeout(() => { inner.style.boxShadow = shadow(grid.rows); }, name === 'wave' ? 700 : 180);
            }, 2000);
        }
    }
    async function drawSprite() {
        const err = $('spriteError');
        err.classList.remove('visible');
        $('spriteStatus').textContent = 'Claude is sketching…';
        $('spriteGenBtn').disabled = true;
        const result = await pywebview.api.build_sprite($('spritePrompt').value)
            .catch(() => ({ ok: false, error: 'bridge error' }));
        $('spriteGenBtn').disabled = false;
        $('spriteStatus').textContent = '';
        if (!result.ok) {
            err.textContent = result.error || 'generation failed';
            err.classList.add('visible');
            return;
        }
        pendingGrid = result.grid;
        $('spriteName').textContent = pendingGrid.name;
        renderGridPreview(pendingGrid);
        $('spritePreviewWrap').classList.add('visible');
    }
    $('spriteGenBtn').addEventListener('click', drawSprite);
    $('spriteRetryBtn').addEventListener('click', drawSprite);
    $('spriteSaveBtn').addEventListener('click', async () => {
        if (!pendingGrid) return;
        const result = await pywebview.api.save_sprite(pendingGrid)
            .catch(() => ({ ok: false, error: 'bridge error' }));
        if (result.ok) {
            $('spriteStatus').textContent = 'Saved - it will wander by soon. Draw another?';
            $('spritePreviewWrap').classList.remove('visible');
            $('spritePrompt').value = '';
            pendingGrid = null;
        } else {
            $('spriteError').textContent = result.error || 'could not save';
            $('spriteError').classList.add('visible');
        }
    });

    // ---- Petdex pets ----

    // Hand-picked original pets from the gallery for one-click adopting.
    const FEATURED_PETS = ['blue-boba-axolotl', 'broom-witch', 'bun', 'brik', 'cantor-sprig'];
    const PET_FRAME_W = 34, PET_FRAME_H = 37; // preview cell (192:208 ratio)

    function petChip(pet) {
        const chip = document.createElement('div');
        chip.className = 'pet-chip';
        const face = document.createElement('div');
        face.className = 'pet-face';
        face.style.width = `${PET_FRAME_W}px`;
        face.style.height = `${PET_FRAME_H}px`;
        face.style.backgroundImage = `url(${pet.sheet})`;
        face.style.backgroundSize = `${PET_FRAME_W * 8}px ${PET_FRAME_H * 9}px`;
        const idleFrames = (pet.rowFrames && pet.rowFrames[0]) || 6;
        let frame = 0;
        const tick = setInterval(() => {
            if (!face.isConnected && frame > 0) { clearInterval(tick); return; }
            frame = (frame + 1) % idleFrames;
            face.style.backgroundPosition = `${-frame * PET_FRAME_W}px 0`;
        }, 160);
        const name = document.createElement('span');
        name.textContent = pet.name;
        chip.append(face, name);
        if (pet.source === 'petdex') {
            const x = document.createElement('button');
            x.className = 'pet-remove';
            x.textContent = '×';
            x.title = 'Remove this pet';
            x.addEventListener('click', async () => {
                renderPets(await pywebview.api.remove_pet(pet.slug).catch(() => []));
            });
            chip.appendChild(x);
        } else {
            chip.title = 'Hatched by the Codex CLI (~/.codex/pets)';
        }
        return chip;
    }

    function renderPets(pets) {
        $('petList').replaceChildren(...(pets || []).map(petChip));
        // Featured picks hide once adopted.
        const have = new Set((pets || []).map((p) => p.slug));
        const chips = FEATURED_PETS.filter((slug) => !have.has(slug)).map((slug) => {
            const b = document.createElement('button');
            b.className = 'pet-featured-chip';
            b.textContent = slug;
            b.addEventListener('click', () => { $('petSlug').value = slug; adoptPet(); });
            return b;
        });
        $('petFeatured').replaceChildren(...chips);
    }

    async function adoptPet() {
        const slug = $('petSlug').value.trim();
        if (!slug) return;
        const err = $('petError');
        err.classList.remove('visible');
        $('petStatus').textContent = 'Adopting…';
        $('petInstallBtn').disabled = true;
        const result = await pywebview.api.install_pet(slug)
            .catch(() => ({ error: 'bridge error' }));
        $('petInstallBtn').disabled = false;
        if (result.error) {
            $('petStatus').textContent = '';
            err.textContent = result.error;
            err.classList.add('visible');
            return;
        }
        $('petStatus').textContent = `${result.pet.name} adopted - it will drop by the HUD soon.`;
        $('petSlug').value = '';
        renderPets(result.pets);
    }

    $('petInstallBtn').addEventListener('click', adoptPet);
    $('petSlug').addEventListener('keydown', (e) => { if (e.key === 'Enter') adoptPet(); });
    $('petdexLink').addEventListener('click', (e) => {
        e.preventDefault();
        pywebview.api.open_petdex();
    });
    if (state.mode === 'settings') {
        pywebview.api.list_pets().then(renderPets).catch(() => {});
    }

    // Poll account status while the window is open (a login can complete
    // in the terminal at any moment).
    setInterval(recheck, 5000);
}

// Compared against the values the app BOOTED with (state.settings), since
// saving alone doesn't apply anything.
function settingsChangedFromBoot() {
    return settingsChanged();
}

async function recheck() {
    try {
        renderAccounts(await pywebview.api.recheck());
    } catch (e) { /* window closing */ }
}

window.addEventListener('pywebviewready', init);
