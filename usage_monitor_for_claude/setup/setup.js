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
        showStep('stepPacks');
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

    // Animated face: pet-list chips animate the sheet's idle ROW (9-row
    // sheet), gallery cards animate the single-row preview STRIP.
    const PET_FRAME_W = 34, PET_FRAME_H = 37; // display cell (192:208 ratio)

    function animatedFace(sheet, idleFrames, sheetRows) {
        const face = document.createElement('div');
        face.className = 'pet-face';
        face.style.width = `${PET_FRAME_W}px`;
        face.style.height = `${PET_FRAME_H}px`;
        face.style.backgroundImage = `url(${sheet})`;
        const cols = sheetRows > 1 ? 8 : idleFrames;
        face.style.backgroundSize = `${PET_FRAME_W * cols}px ${PET_FRAME_H * sheetRows}px`;
        let frame = 0;
        const tick = setInterval(() => {
            if (!face.isConnected && frame > 0) { clearInterval(tick); return; }
            frame = (frame + 1) % (idleFrames || 1);
            face.style.backgroundPosition = `${-frame * PET_FRAME_W}px 0`;
        }, 160);
        return face;
    }

    function petChip(pet) {
        const chip = document.createElement('div');
        chip.className = 'pet-chip';
        const face = animatedFace(pet.sheet, (pet.rowFrames && pet.rowFrames[0]) || 6, pet.sheetRows || 9);
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
                renderGallery();
            });
            chip.appendChild(x);
        } else {
            chip.title = 'Hatched by the Codex CLI (~/.codex/pets)';
        }
        return chip;
    }

    let installedSlugs = new Set();
    function renderPets(pets) {
        installedSlugs = new Set((pets || []).map((p) => p.slug));
        $('petList').replaceChildren(...(pets || []).map(petChip));
    }

    async function adoptOne(slug) {
        const result = await pywebview.api.install_pet(slug)
            .catch(() => ({ error: 'bridge error' }));
        if (!result.error) renderPets(result.pets);
        return result;
    }

    async function adoptPet() {
        const slug = $('petSlug').value.trim();
        if (!slug) return;
        const err = $('petError');
        err.classList.remove('visible');
        $('petStatus').textContent = 'Adopting…';
        $('petInstallBtn').disabled = true;
        const result = await adoptOne(slug);
        $('petInstallBtn').disabled = false;
        if (result.error) {
            $('petStatus').textContent = '';
            err.textContent = result.error;
            err.classList.add('visible');
            return;
        }
        $('petStatus').textContent = `${result.pet.name} adopted - it will drop by the HUD soon.`;
        $('petSlug').value = '';
        renderGallery();
    }

    // Install every pet of a collection, narrating progress into `status`.
    async function adoptPack(pack, status, setBusy) {
        setBusy(true);
        status.textContent = `Loading ${pack.name || pack.slug}…`;
        const listed = await pywebview.api.pack_pets(pack.slug).catch(() => ({ error: 'bridge error' }));
        if (listed.error) {
            status.textContent = listed.error;
            setBusy(false);
            return 0;
        }
        let ok = 0;
        for (let i = 0; i < listed.pets.length; i++) {
            const slug = listed.pets[i];
            status.textContent = `${pack.name || pack.slug}: adopting ${slug} (${i + 1}/${listed.pets.length})…`;
            const r = await adoptOne(slug);
            if (!r.error) ok++;
        }
        status.textContent = `${pack.name || pack.slug}: ${ok} of ${listed.pets.length} pets adopted.`;
        setBusy(false);
        renderGallery();
        return ok;
    }

    function renderPackChips() {
        const chips = (state.packs || []).map((pack) => {
            const b = document.createElement('button');
            b.className = 'pet-featured-chip';
            b.textContent = `${pack.name} pack`;
            b.title = pack.blurb || '';
            b.addEventListener('click', () => adoptPack(pack, $('petStatus'), (busy) => { b.disabled = busy; }));
            return b;
        });
        $('petPacks').replaceChildren(...chips);
    }

    // -- Full-gallery browser (index from the petdex sitemap, lazy previews) --

    let galleryIndex = null;      // [{slug, name}] or null until loaded
    let galleryShown = 24;
    const previews = new Map();   // slug -> preview payload | 'failed'
    let previewPump = false;

    function galleryCard(entry) {
        const card = document.createElement('div');
        card.className = 'pet-card';
        card.dataset.slug = entry.slug;
        const facePocket = document.createElement('div');
        facePocket.className = 'pet-card-pocket';
        const p = previews.get(entry.slug);
        if (p && p !== 'failed') facePocket.appendChild(animatedFace(p.sheet, p.frames, 1));
        const name = document.createElement('span');
        name.className = 'pet-card-name';
        name.textContent = entry.name;
        card.append(facePocket, name);
        if (installedSlugs.has(entry.slug)) {
            const owned = document.createElement('span');
            owned.className = 'pet-card-owned';
            owned.textContent = 'adopted ✓';
            card.appendChild(owned);
        } else {
            const b = document.createElement('button');
            b.className = 'pet-card-adopt';
            b.textContent = 'Adopt';
            b.addEventListener('click', async () => {
                b.disabled = true;
                b.textContent = '…';
                const r = await adoptOne(entry.slug);
                if (r.error) { b.disabled = false; b.textContent = 'Adopt'; }
                else renderGallery();
            });
            card.appendChild(b);
        }
        return card;
    }

    function galleryMatches() {
        const q = $('gallerySearch').value.trim().toLowerCase();
        const all = galleryIndex || [];
        return q ? all.filter((p) => p.slug.includes(q)) : all;
    }

    function renderGallery() {
        if (galleryIndex === null || !$('galleryPanel').classList.contains('open')) return;
        const matches = galleryMatches();
        $('galleryGrid').replaceChildren(...matches.slice(0, galleryShown).map(galleryCard));
        $('galleryMore').style.display = matches.length > galleryShown ? '' : 'none';
        pumpPreviews();
    }

    // Fetch previews for visible cards a few at a time; slot each into its
    // card as it lands (cards re-query by slug so re-renders are safe).
    async function pumpPreviews() {
        if (previewPump) return;
        previewPump = true;
        try {
            for (;;) {
                const card = [...document.querySelectorAll('#galleryGrid .pet-card')]
                    .find((c) => !previews.has(c.dataset.slug));
                if (!card) break;
                const slug = card.dataset.slug;
                const r = await pywebview.api.pet_preview(slug).catch(() => ({ error: 1 }));
                previews.set(slug, r.error ? 'failed' : r.preview);
                const live = document.querySelector(`#galleryGrid .pet-card[data-slug="${slug}"] .pet-card-pocket`);
                if (live && !r.error) live.replaceChildren(animatedFace(r.preview.sheet, r.preview.frames, 1));
            }
        } finally {
            previewPump = false;
        }
    }

    async function openGallery() {
        const panel = $('galleryPanel');
        panel.classList.toggle('open');
        $('browseToggle').textContent = panel.classList.contains('open')
            ? 'hide the gallery' : 'browse the full gallery…';
        if (!panel.classList.contains('open')) return;
        if (galleryIndex === null) {
            $('petStatus').textContent = 'Loading the gallery…';
            const r = await pywebview.api.browse_pets().catch(() => ({ error: 'bridge error' }));
            $('petStatus').textContent = '';
            if (r.error) {
                $('petError').textContent = r.error;
                $('petError').classList.add('visible');
                panel.classList.remove('open');
                return;
            }
            galleryIndex = r.pets;
            $('gallerySearch').placeholder = `search ${galleryIndex.length} pets…`;
        }
        renderGallery();
    }

    $('petInstallBtn').addEventListener('click', adoptPet);
    $('petSlug').addEventListener('keydown', (e) => { if (e.key === 'Enter') adoptPet(); });
    $('browseToggle').addEventListener('click', openGallery);
    $('gallerySearch').addEventListener('input', () => { galleryShown = 24; renderGallery(); });
    $('galleryMore').addEventListener('click', () => { galleryShown += 24; renderGallery(); });
    for (const id of ['petdexLink', 'petdexLinkWizard']) {
        $(id).addEventListener('click', (e) => {
            e.preventDefault();
            pywebview.api.open_petdex();
        });
    }
    if (state.mode === 'settings') {
        pywebview.api.list_pets().then(renderPets).catch(() => {});
        renderPackChips();
    }

    // -- Onboarding starter packs --

    function renderPackChoices() {
        const rows = (state.packs || []).map((pack, i) => {
            const label = document.createElement('label');
            label.className = 'check pack-choice';
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.value = pack.slug;
            box.checked = i === 0; // the originals pack is the default
            const text = document.createElement('span');
            text.innerHTML = `<b>${pack.name}</b> <span class="dim">— ${pack.blurb || ''}</span>`;
            label.append(box, text);
            return label;
        });
        $('packChoices').replaceChildren(...rows);
    }
    renderPackChoices();

    $('packSkipBtn').addEventListener('click', () => showStep('stepDone'));
    $('packAdoptBtn').addEventListener('click', async () => {
        const chosen = (state.packs || []).filter((pack) =>
            $('packChoices').querySelector(`input[value="${pack.slug}"]`)?.checked);
        if (!chosen.length) { showStep('stepDone'); return; }
        $('packAdoptBtn').disabled = true;
        $('packSkipBtn').disabled = true;
        let total = 0;
        for (const pack of chosen) {
            total += await adoptPack(pack, $('packStatus'), () => {});
        }
        $('packStatus').textContent = `${total} pets adopted - they'll start visiting the HUD.`;
        setTimeout(() => showStep('stepDone'), 1200);
    });

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
