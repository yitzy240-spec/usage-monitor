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
    const appLogin = !!accounts.claude_app_login;
    $('accClaude').querySelector('.acc-status').textContent =
        accounts.claude ? (appLogin ? 'signed in (app login)' : 'signed in') : 'not signed in';
    $('signOutRow').classList.toggle('app-login', appLogin);
    if (accounts.claude) $('oauthCodeRow').classList.remove('visible');
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

    // App login (OAuth) for claude.ai users without the CLI.
    async function startAppLogin() {
        $('oauthError').textContent = '';
        const started = await pywebview.api.claude_login_start().catch(() => false);
        if (started) {
            $('oauthCodeRow').classList.add('visible');
            $('oauthCode').focus();
        } else {
            $('oauthError').textContent = 'Could not open the browser.';
            $('oauthError').classList.add('visible');
        }
    }
    $('claudeOauthBtn').addEventListener('click', startAppLogin);
    $('claudeAltLogin').addEventListener('click', () => {
        // Reveal the sign-in flow on an already-signed-in (CLI) card.
        $('accClaude').querySelectorAll('.acc-actions').forEach((el) => el.classList.add('revealed'));
        startAppLogin();
    });
    $('oauthConfirm').addEventListener('click', async () => {
        const result = await pywebview.api.claude_login_finish($('oauthCode').value)
            .catch(() => ({ ok: false, error: 'bridge error' }));
        const err = $('oauthError');
        err.classList.toggle('visible', !result.ok);
        err.textContent = result.ok ? '' : (result.error || 'sign-in failed');
        if (result.ok) {
            $('oauthCode').value = '';
            recheck();
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
