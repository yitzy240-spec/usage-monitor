let state = null;
let dirty = false;

const $ = (id) => document.getElementById(id);

function showStep(id) {
    for (const step of document.querySelectorAll('.step')) {
        step.classList.toggle('active', step.id === id);
    }
}

function renderAccounts(accounts) {
    for (const [key, el] of [['claude', $('accClaude')], ['codex', $('accCodex')]]) {
        const ok = !!accounts[key];
        el.classList.toggle('ok', ok);
        el.classList.toggle('missing', !ok);
        el.querySelector('.acc-status').textContent = ok ? 'signed in' : 'not signed in';
    }
}

function collectSettings() {
    return {
        hud_hotkey: $('hotkeyInput').value.trim(),
        hud_linger: Number($('lingerInput').value),
        hud_sessions: $('sessionsInput').checked,
        codex_enabled: $('codexInput').checked,
    };
}

function settingsChanged() {
    const now = collectSettings();
    const was = state.settings;
    return now.hud_hotkey !== was.hud_hotkey
        || now.hud_linger !== was.hud_linger
        || now.hud_sessions !== was.hud_sessions
        || now.codex_enabled !== was.codex_enabled;
}

async function validateHotkey() {
    const input = $('hotkeyInput');
    const ok = await pywebview.api.check_hotkey(input.value.trim());
    input.classList.toggle('invalid', !ok);
    $('hotkeyError').classList.toggle('visible', !ok);
    return ok;
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
    $('lingerInput').value = state.settings.hud_linger;
    $('lingerVal').textContent = `${state.settings.hud_linger}s`;
    $('sessionsInput').checked = state.settings.hud_sessions;
    $('codexInput').checked = state.settings.codex_enabled;
    $('autostartField').style.display = state.frozen ? '' : 'none';
    $('autostartInput').checked = state.autostart;

    showStep(state.mode === 'onboarding' ? 'stepWelcome' : 'stepAccounts');
    if (state.mode === 'settings') {
        // Settings mode shows accounts + prefs as one page.
        $('stepPrefs').classList.add('active');
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
    $('lingerInput').addEventListener('input', () => {
        $('lingerVal').textContent = `${$('lingerInput').value}s`;
    });
    $('hotkeyInput').addEventListener('input', validateHotkey);
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
