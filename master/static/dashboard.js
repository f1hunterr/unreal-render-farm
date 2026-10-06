// Unreal Render Farm dashboard. Runs under a strict CSP: no inline scripts, handlers or style attributes.
"use strict";

const $ = (id) => document.getElementById(id);

// Everything that came from a node, the registry, the queue or the history is untrusted: escape it.
function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
}

function pct(value) {
    return value === null || value === undefined ? 'n/a' : `${num(value)}%`;
}

async function getJson(url) {
    const res = await fetch(url);
    return res.json();
}

async function postJson(url, body) {
    const res = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {})
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    return {ok: res.ok, data};
}

// Progress widths are applied through the CSSOM (allowed by CSP) instead of style="" attributes
function applyWidths(root) {
    root.querySelectorAll('[data-width]').forEach(el => {
        el.style.width = `${Math.min(100, Math.max(0, num(el.dataset.width)))}%`;
    });
}

function shortTime(iso) {
    if (!iso) return '';
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return '';
    const sameDay = when.toDateString() === new Date().toDateString();
    const time = when.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    return sameDay ? time : `${when.toLocaleDateString()} ${time}`;
}

function fileName(path) {
    return String(path || '').split(/[\\/]/).pop();
}

// ------------------------------ Notices and confirmations
function toast(message, kind = 'ok') {
    const box = document.createElement('div');
    box.className = `toast toast-${kind}`;
    const icon = {ok: 'fa-check-circle', warn: 'fa-exclamation-triangle', error: 'fa-times-circle'}[kind] || 'fa-info-circle';
    box.innerHTML = `<i class="fas ${icon}"></i><span>${esc(message)}</span>`;
    $('toasts').appendChild(box);
    setTimeout(() => box.classList.add('toast-hide'), kind === 'error' ? 8000 : 4500);
    setTimeout(() => box.remove(), kind === 'error' ? 8600 : 5100);
}

function askConfirm(text, yesLabel = 'Yes') {
    return new Promise(resolve => {
        const modal = $('confirm');
        $('confirm-text').textContent = text;
        $('confirm-yes').textContent = yesLabel;
        modal.classList.remove('hidden');
        $('confirm-yes').focus();
        const done = (answer) => {
            modal.classList.add('hidden');
            $('confirm-yes').removeEventListener('click', yes);
            $('confirm-no').removeEventListener('click', no);
            document.removeEventListener('keydown', key);
            resolve(answer);
        };
        const yes = () => done(true), no = () => done(false);
        const key = (e) => { if (e.key === 'Escape') done(false); };
        $('confirm-yes').addEventListener('click', yes);
        $('confirm-no').addEventListener('click', no);
        document.addEventListener('keydown', key);
    });
}

// ------------------------------ Remembered form values (this browser only)
const PREF_KEY = 'urf.form.v1';
const PREF_FIELDS = ['project', 'map', 'config', 'priority', 'retries', 'chunk-size', 'warmup'];
const PREF_CHECKS = ['auto-split'];

function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}'); } catch (e) { return {}; }
}

function savePrefs(extra) {
    try {
        const prefs = loadPrefs();
        PREF_FIELDS.forEach(id => { prefs[id] = $(id).value; });
        PREF_CHECKS.forEach(id => { prefs[id] = $(id).checked; });
        Object.assign(prefs, extra || {});
        localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch (e) { /* private window or storage blocked: fine */ }
}

function restorePrefs() {
    const prefs = loadPrefs();
    PREF_FIELDS.forEach(id => { if (prefs[id] !== undefined && prefs[id] !== '') $(id).value = prefs[id]; });
    PREF_CHECKS.forEach(id => { if (typeof prefs[id] === 'boolean') $(id).checked = prefs[id]; });
    excluded = new Set(Array.isArray(prefs.excluded) ? prefs.excluded : []);
}

// ------------------------------ Tabs
let activeTab = 'dashboard';

function switchTab(tabName, btn) {
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    $(tabName).classList.add('active');
    btn.classList.add('active');
    activeTab = tabName;
    if (tabName === 'history') loadHistory();
    if (tabName === 'queue') loadQueue();
}

// ------------------------------ Shots in the form
function addSequence() {
    const row = document.createElement("div");
    row.className = "seq-row";
    row.innerHTML = `
        <input type="text" class="sequence-input" placeholder="e.g. /Game/Sequences/Shot010.Shot010" aria-label="Level Sequence">
        <input type="text" class="frames-input" placeholder="e.g. 0-999" aria-label="Frames (optional)">
        <div class="seq-actions">
            <button class="btn ready-toggle is-ready btn-small" data-action="ready" title="Click to skip this shot">
                <i class="fas fa-check"></i> Ready
            </button>
            <button class="btn btn-secondary btn-small icon-btn" data-action="up" title="Move up" aria-label="Move up">
                <i class="fas fa-arrow-up"></i>
            </button>
            <button class="btn remove-btn btn-small icon-btn" data-action="remove" title="Remove" aria-label="Remove">
                <i class="fas fa-times"></i>
            </button>
        </div>
    `;
    $("sequence-container").appendChild(row);
    return row;
}

function toggleReady(btn) {
    btn.classList.toggle('is-ready');
    const ready = btn.classList.contains('is-ready');
    btn.innerHTML = ready ? '<i class="fas fa-check"></i> Ready' : '<i class="fas fa-pause"></i> Skip';
    btn.title = ready ? 'Click to skip this shot' : 'Click to render this shot';
    btn.closest('.seq-row').classList.toggle('skipped', !ready);
}

function onSequenceClick(e) {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const row = btn.closest('.seq-row');
    if (btn.dataset.action === 'ready') toggleReady(btn);
    if (btn.dataset.action === 'remove') {
        row.remove();
        if (!document.querySelector('.seq-row')) addSequence();
    }
    if (btn.dataset.action === 'up' && row.previousElementSibling) {
        row.parentElement.insertBefore(row, row.previousElementSibling);
    }
}

// ------------------------------ Computers (registry + live state)
let registry = {};          // name -> {ip}
let statuses = {};          // name -> latest status from /status
let excluded = new Set();   // computers the user switched off for their renders

function nodeState(name) {
    const stage = (statuses[name] || {}).stage || 'CONNECTING';
    if (stage === 'IDLE') return 'idle';
    if (['INITIALIZING', 'RENDERING', 'CANCELLING'].includes(stage)) return 'busy';
    if (stage === 'CONNECTING') return 'connecting';
    return 'offline';
}

const STATE_WORDS = {idle: 'free', busy: 'rendering', offline: 'offline', connecting: 'checking…'};

function renderPills() {
    const names = Object.keys(registry).sort();
    $("node-list").innerHTML = names.length ? names.map(n => {
        const state = nodeState(n), on = !excluded.has(n);
        return `
            <button class="node-pill ${on ? 'active' : 'off'}" data-node="${esc(n)}"
                    title="${on ? 'Used for this render. Click to leave it out' : 'Left out. Click to use it'}">
                <span class="dot dot-${state}"></span>
                <span>${esc(n)}</span>
                <small class="muted">${STATE_WORDS[state]}</small>
                ${isOldAgent(n) ? '<small class="old-tag" title="Run the latest SETUP.bat on this computer">update</small>' : ''}
                <i class="fas ${on ? 'fa-check-square' : 'fa-square'} pick"></i>
            </button>`;
    }).join('') : '<p class="muted-small">No computers registered yet.</p>';
}

function renderAdminNodes() {
    const names = Object.keys(registry).sort();
    $("admin-nodes").innerHTML = names.map(n => {
        const state = nodeState(n);
        return `
            <tr>
                <td class="nowrap"><span class="dot dot-${state}"></span> ${esc(n)}</td>
                <td class="mono">${esc(registry[n].ip)}</td>
                <td>${esc((statuses[n] || {}).stage || 'CONNECTING')}</td>
                <td>${agentLabel(n)}</td>
                <td><button class="btn btn-secondary btn-small" data-remove="${esc(n)}">Remove</button></td>
            </tr>`;
    }).join('') || '<tr><td colspan="5" class="muted">No computers registered yet.</td></tr>';
}

function isOldAgent(name) {
    const s = statuses[name];
    const needed = ['frame_range', 'auto_piece'];  // abilities of the current agent
    return Boolean(s && s.stage !== 'CONNECTING' && needed.some(f => !(s.features || []).includes(f))
                   && (s.agent_version || s.stage !== 'OFFLINE'));
}

function agentLabel(name) {
    const s = statuses[name] || {};
    if (isOldAgent(name)) return '<span class="status-tag tag-fail" title="Run the latest SETUP.bat on this computer">old - update</span>';
    return esc(s.agent_version || '—');
}

const OLD_AGENT_LINE = '<div class="scene-info old-agent"><i class="fas fa-exclamation-triangle"></i> Old agent: it cannot take shared shots. Run the latest SETUP.bat on this computer.</div>';

async function loadNodes() {
    registry = await getJson('/get-nodes');
    renderPills();
    renderAdminNodes();
}

function onNodeListClick(e) {
    const pill = e.target.closest('.node-pill');
    if (!pill) return;
    const name = pill.dataset.node;
    if (excluded.has(name)) excluded.delete(name); else excluded.add(name);
    savePrefs({excluded: [...excluded]});
    renderPills();
}

async function onAdminNodesClick(e) {
    const btn = e.target.closest('button[data-remove]');
    if (!btn) return;
    const name = btn.dataset.remove;
    if (!await askConfirm(`Remove ${name} from the farm? A render running there is put back in the queue.`, 'Remove')) return;
    const {ok, data} = await postJson('/remove-node', {name});
    if (ok) toast(`${name} removed`); else toast(data.error || 'Could not remove the computer', 'error');
    loadNodes();
}

async function addNode() {
    const name = $("node-name").value.trim();
    const ip = $("node-ip").value.trim();
    if (!name || !ip) {
        toast('Enter both the computer name and its IP address', 'warn');
        return;
    }
    const {ok, data} = await postJson('/add-node', {name, ip});
    if (!ok) {
        toast(data.error || 'Could not add the computer', 'error');
        return;
    }
    $("node-name").value = '';
    $("node-ip").value = '';
    toast(`${name} added`);
    loadNodes();
    updateStatus();
}

// ------------------------------ Send a render
async function launch() {
    const rows = Array.from(document.querySelectorAll(".seq-row"));
    const readySeqs = rows
        .filter(row => row.querySelector(".ready-toggle").classList.contains("is-ready"))
        .map(row => ({
            path: row.querySelector(".sequence-input").value.trim(),
            frames: row.querySelector(".frames-input").value.trim(),
        }))
        .filter(s => s.path !== "");
    const selected = Object.keys(registry).filter(n => !excluded.has(n));
    const project = $('project').value.trim();
    const map = $('map').value.trim();
    const config = $('config').value.trim();

    if (!project || !map || !config) {
        toast('Fill in the project file, map and render preset first', 'warn');
        return;
    }
    if (readySeqs.length === 0) {
        toast('Add at least one shot marked Ready', 'warn');
        return;
    }
    if (selected.length === 0) {
        toast('Pick at least one computer to render on', 'warn');
        return;
    }

    const btn = $('launch');
    btn.disabled = true;
    const {ok, data} = await postJson('/launch', {
        project, map, config,
        sequences: readySeqs,
        nodes: selected,
        priority: num($('priority').value),
        retries: num($('retries').value),
        chunk_size: num($('chunk-size').value),
        warmup: num($('warmup').value),
        auto_split: $('auto-split').checked,
        output_dir: $('output-dir').value.trim(),
    });
    btn.disabled = false;
    if (!ok) {
        toast(data.error || 'Could not send the render', 'error');
        return;
    }
    savePrefs();
    const share = data.sharing || {};
    const sharers = share.computers || [];
    if ($('auto-split').checked && share.computers) {
        toast(sharers.length > 1
            ? `Sent ${data.shots} shot${data.shots === 1 ? '' : 's'}, shared between ${sharers.join(', ')}.`
            : `Sent ${data.shots} shot${data.shots === 1 ? '' : 's'}. Only ${sharers[0] || 'one computer'} can take it, so it renders whole.`);
        (share.left_out || []).forEach(x => toast(`${x.node} is not sharing: it ${x.why}.`, 'warn'));
    } else {
        const split = data.jobs > data.shots ? ` as ${data.jobs} pieces` : '';
        toast(`Sent ${data.shots} shot${data.shots === 1 ? '' : 's'}${split}. Free computers start by themselves.`);
        if (data.sharing_off && selected.length > 1 && data.jobs < selected.length) {
            toast('Sharing is off, so one computer renders each shot. Tick "Share each shot between free computers" to use all the computers you selected.', 'warn');
        }
    }
    loadQueue();
}

async function prepareProject() {
    const whole = $('prepare-whole').checked;
    const project = $('project').value.trim();
    const map = $('map').value.trim();
    const config = $('config').value.trim();
    const sequences = Array.from(document.querySelectorAll(".seq-row .sequence-input"))
        .map(input => input.value.trim()).filter(Boolean);
    const selected = Object.keys(registry).filter(n => !excluded.has(n));
    if (!project || (!whole && (!map || !config))) {
        toast(whole ? 'Fill in the project file first' : 'Fill in the project file, map and render preset first', 'warn');
        return;
    }
    if (!whole && sequences.length === 0) {
        toast('Add the shots you want to prepare', 'warn');
        return;
    }
    if (whole && !await askConfirm('Prepare the WHOLE project? Unreal builds everything in it, which can take hours. ' +
                                   'Usually "Prepare project first" without this tick is enough.', 'Prepare whole project')) {
        return;
    }
    const btn = $('prepare');
    btn.disabled = true;
    const {ok, data} = await postJson('/launch', {
        project, map, config, nodes: selected,
        sequences: whole ? ['WholeProject'] : sequences,
        prepare: whole ? 'whole' : 'quick',
        retries: num($('retries').value),
    });
    btn.disabled = false;
    if (!ok) {
        toast(data.error || 'Could not prepare the project', 'error');
        return;
    }
    savePrefs();
    toast(whole ? 'Preparing the whole project. It runs first; you can send renders now and they wait for a free computer.'
                : `Preparing ${data.jobs} shot${data.jobs === 1 ? '' : 's'}: 1 test frame per camera cut. Send your render now; it starts faster after this.`);
    if (!(data.computers || []).length) {
        toast('None of the selected computers can prepare yet: run the latest SETUP.bat on them.', 'warn');
    }
    loadQueue();
}

// ------------------------------ Shared cache (Admin)
async function loadSettings() {
    try {
        const data = await getJson('/get-settings');
        $('shared-ddc').value = data.shared_ddc || '';
        $('output-root').value = data.output_root || '';
    } catch (e) {
        console.error('Settings load failed:', e);
    }
}

async function saveSharedCache() {
    const {ok, data} = await postJson('/save-settings', {shared_ddc: $('shared-ddc').value.trim()});
    if (!ok) {
        toast(data.error || 'Could not save', 'error');
        return;
    }
    $('shared-ddc').value = data.shared_ddc;
    toast(data.shared_ddc ? 'Saved. New renders use the shared cache.' : 'Shared cache turned off.');
}

async function saveOutputRoot() {
    const {ok, data} = await postJson('/save-settings', {output_root: $('output-root').value.trim()});
    if (!ok) {
        toast(data.error || 'Could not save', 'error');
        return;
    }
    $('output-root').value = data.output_root;
    toast(data.output_root ? `Saved. New renders save their frames in ${data.output_root}\\<project>\\<shot>.`
                           : 'Turned off: renders use their preset\'s folder.');
}

async function checkSharedCache() {
    const list = $('ddc-results');
    list.innerHTML = '<li class="muted">Asking every computer…</li>';
    const {ok, data} = await postJson('/check-cache', {path: $('shared-ddc').value.trim()});
    if (!ok) {
        list.innerHTML = '';
        toast(data.error || 'Could not test the folder', 'error');
        return;
    }
    const results = data.results || [];
    list.innerHTML = results.length ? results.map(r => `
        <li><span class="status-tag ${r.ok ? 'tag-success' : 'tag-fail'}">${r.ok ? 'OK' : 'NO'}</span>
            <b>${esc(r.node)}</b> ${r.ok ? 'can write to the cache' : esc(r.error)}</li>`).join('')
        : '<li class="muted">No computers registered.</li>';
}

// ------------------------------ Live status
const BADGES = {
    IDLE: "badge-idle",
    INITIALIZING: "badge-initializing",
    RENDERING: "badge-rendering",
    CANCELLING: "badge-initializing",
    CONNECTING: "badge-idle",
};

function lastResultLine(r) {
    if (!r) return '';
    const cls = r.status === 'COMPLETED' ? 'tag-success' : 'tag-fail';
    return `
        <div class="scene-info last-result" title="${esc(r.detail)}">
            Last: <span class="status-tag ${cls}">${esc(r.status)}</span>
            ${esc(r.scene)} · ${esc(r.duration)}
        </div>`;
}

function offlineCard(n) {
    const authError = n.stage === 'AUTH ERROR';
    const since = shortTime(n.offline_since);
    const help = authError
        ? 'Its farm password (token) does not match. Ask the farm admin to run SETUP.bat again.'
        : 'Check the computer is switched on and logged in.';
    return `
        <div class="node-card offline compact">
            <div class="node-header">
                <div class="node-name"><i class="fas fa-desktop"></i> ${esc(n.node)}</div>
                <span class="status-badge badge-offline">${esc(n.stage)}</span>
            </div>
            <div class="scene-info">${since ? `Unreachable since ${esc(since)}. ` : ''}${help}</div>
        </div>`;
}

function fmtElapsed(seconds) {
    const s = Math.max(0, Math.floor(num(seconds)));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(r).padStart(2, '0')}s`;
}

function loadingBlock(n) {
    // Before Unreal renders its first frame there is no frame progress; show what it is doing instead
    return `
        <div class="loading-state">
            <div class="progress-bar-wrapper"><div class="progress-bar indeterminate"></div></div>
            <div class="progress-text">
                <span><i class="fas fa-circle-notch fa-spin"></i> ${esc(n.activity || 'Opening Unreal and loading the project')}</span>
                <span><i class="fas fa-clock"></i> ${fmtElapsed(n.elapsed)}</span>
            </div>
            <div class="loading-hint">The first render of a project on a computer can take 10+ minutes while Unreal builds meshes and shaders.</div>
        </div>`;
}

function nodeCard(n) {
    if (!(n.stage in BADGES)) return offlineCard(n);
    const busy = ["INITIALIZING", "RENDERING", "CANCELLING"].includes(n.stage);
    const badgeClass = BADGES[n.stage];
    if (!busy) {
        return `
            <div class="node-card compact">
                <div class="node-header">
                    <div class="node-name"><i class="fas fa-desktop"></i> ${esc(n.node)}</div>
                    <span class="status-badge ${badgeClass}">${n.stage === 'IDLE' ? 'FREE' : esc(n.stage)}</span>
                </div>
                <div class="scene-info">${n.stage === 'IDLE' ? '<i class="fas fa-check-circle"></i> Ready for work' : 'Checking…'}</div>
                ${isOldAgent(n.node) ? OLD_AGENT_LINE : ''}
                ${lastResultLine(n.last_result)}
            </div>`;
    }
    return `
        <div class="node-card rendering">
            <div class="node-header">
                <div class="node-name"><i class="fas fa-desktop"></i> ${esc(n.node)}</div>
                <span class="status-badge ${badgeClass}">${esc(n.stage)}</span>
            </div>
            <div class="scene-info">${n.scene ? `<i class="fas fa-film"></i> ${esc(n.scene)}` : ''}</div>
            ${!num(n.progress) && !num(n.current_frame) && n.stage !== 'CANCELLING' ? loadingBlock(n) : `
            <div class="progress-container">
                <div class="progress-bar-wrapper">
                    <div class="progress-bar" data-width="${num(n.progress)}"></div>
                </div>
                <div class="progress-text">
                    <span><i class="fas fa-chart-line"></i> ${num(n.progress)}% done</span>
                    <span><i class="fas fa-clock"></i> ${esc(n.eta || 'Starting…')}</span>
                </div>
                <div class="progress-text">
                    <span>Frame ${num(n.current_frame)} / ${num(n.total_frames)}</span>
                    <span>${num(n.fps)} fps · ${fmtElapsed(n.elapsed)}</span>
                </div>
            </div>`}
            <div class="stats-grid">
                <div class="stat-item"><div class="stat-label">CPU</div><div class="stat-value stat-cpu">${pct(n.cpu_usage)}</div></div>
                <div class="stat-item"><div class="stat-label">GPU</div><div class="stat-value stat-gpu">${pct(n.gpu_usage)}</div></div>
                <div class="stat-item"><div class="stat-label">RAM</div><div class="stat-value stat-ram">${pct(n.ram_usage)}</div></div>
                <div class="stat-item"><div class="stat-label">VRAM</div><div class="stat-value stat-vram">${pct(n.vram_usage)}</div></div>
            </div>
            <button class="btn btn-danger btn-block btn-small cancel-node spaced-top" data-node="${esc(n.node)}">
                <i class="fas fa-stop"></i> Stop Render
            </button>
        </div>`;
}

let uiBuild = null;
function checkUiBuild(res) {
    // The master stamps every reply with the dashboard version; a change means this page is out of date
    const build = res.headers.get('X-Farm-UI');
    if (!build) return;
    if (uiBuild === null) uiBuild = build;
    else if (build !== uiBuild) $('reload-bar').classList.remove('hidden');
}

async function updateStatus() {
    try {
        const res = await fetch('/status');
        checkUiBuild(res);
        const nodes = await res.json();
        statuses = Object.fromEntries(nodes.map(n => [n.node, n]));
        const rendering = nodes.filter(n => n.stage === "RENDERING").length;
        // Rendering computers first, then free ones, then offline
        const order = {busy: 0, idle: 1, connecting: 2, offline: 3};
        nodes.sort((a, b) => order[nodeState(a.node)] - order[nodeState(b.node)] || a.node.localeCompare(b.node));

        const grid = $("node-status-grid");
        grid.innerHTML = nodes.map(nodeCard).join('');
        applyWidths(grid);
        $("no-nodes").classList.toggle('hidden', nodes.length > 0);
        $("active-count").innerHTML = `
            <i class="fas fa-circle ${rendering > 0 ? 'indicator-on' : 'indicator-off'}"></i>
            ${rendering} rendering · ${nodes.length} computer${nodes.length === 1 ? '' : 's'}`;
        if (nodes.some(n => !(n.node in registry))) {
            loadNodes();  // a computer registered itself since the page loaded
        } else {
            renderPills();
            if (activeTab === 'admin') renderAdminNodes();
            if (retryTarget) renderRetryNodes();  // keep the Edit window's computer states live
        }
    } catch (e) {
        console.error('Status update failed:', e);
    }
}

async function onStatusGridClick(e) {
    const btn = e.target.closest('.cancel-node');
    if (!btn) return;
    const node = btn.dataset.node;
    if (!await askConfirm(`Stop the render on ${node}? That shot is marked cancelled.`, 'Stop render')) return;
    const {ok, data} = await postJson('/cancel-node', {node});
    if (ok) toast(`Stopping ${node}…`); else toast(data.error || `Could not reach ${node}`, 'error');
    updateStatus();
}

// ------------------------------ Queue
const PRIORITIES = {0: 'Rush', 1: 'Normal', 2: 'Low'};

function queueTag(status) {
    return {
        SUCCESS: 'tag-success', ASSIGNED: 'tag-dispatched', QUEUED: 'tag-queued',
        CANCELLED: 'tag-muted', FAILED: 'tag-fail'
    }[status] || 'tag-muted';
}

function queueActions(job) {
    if (job.status === 'QUEUED' || job.status === 'ASSIGNED') {
        return `<button class="btn btn-secondary btn-small" data-job="${num(job.id)}" data-action="cancel">Cancel</button>`;
    }
    if (job.status === 'FAILED' || job.status === 'CANCELLED') {
        return `<div class="row-actions">
            <button class="btn btn-secondary btn-small" data-job="${num(job.id)}" data-action="retry" title="Try again with the same settings">Retry</button>
            <button class="btn btn-secondary btn-small" data-job="${num(job.id)}" data-action="edit" title="Change settings or computers, then retry"><i class="fas fa-pen"></i> Edit</button>
        </div>`;
    }
    return '';
}

function frameText(job) {
    if (job.kind === 'prepare') return '<span class="muted" title="1 test frame per camera cut, to build the cache">prepare</span>';
    if (job.kind === 'prepare-fill') return '<span class="muted" title="Epic\'s cache fill for the whole project">whole project</span>';
    if ((job.split_mode === 'auto' || job.split_mode === 'auto-piece') && (job.frame_start === null || job.frame_start === undefined)) {
        const piece = num(job.chunk_count) > 1 ? ` (piece ${num(job.chunk_index)}/${num(job.chunk_count)})` : '';
        return `<span class="muted" title="The computer reads the shot's frames when Unreal has loaded">auto…${piece}</span>`;
    }
    if (job.frame_start === null || job.frame_start === undefined) return 'all';
    const chunk = num(job.chunk_count) > 1 ? ` <span class="muted">(${num(job.chunk_index)}/${num(job.chunk_count)})</span>` : '';
    return `${num(job.frame_start)}–${num(job.frame_end)}${chunk}`;
}

function statusCell(job) {
    const live = job.status === 'ASSIGNED' && job.progress !== null && job.progress !== undefined;
    return `
        <span class="status-tag ${queueTag(job.status)}">${esc(job.status)}</span>
        ${live ? `<div class="mini-progress" title="${num(job.progress)}% done"><div class="progress-bar" data-width="${num(job.progress)}"></div></div>` : ''}`;
}

function shotName(path) {
    return String(path || '').split('/').pop().split('.')[0] || path;
}

function renderShots(shots) {
    $("shots-card").classList.toggle('empty', shots.length === 0);
    const grid = $("shots-table");
    grid.innerHTML = shots.map(shot => {
        const total = num(shot.chunks), done = num(shot.done);
        const open = num(shot.queued) + num(shot.rendering);
        const parts = [
            `${done}/${total} done`,
            shot.rendering ? `${num(shot.rendering)} rendering` : '',
            shot.queued ? `${num(shot.queued)} waiting` : '',
            shot.failed ? `<span class="status-tag tag-fail">${num(shot.failed)} failed</span>` : '',
            shot.cancelled ? `${num(shot.cancelled)} cancelled` : '',
            (shot.frames_expected && num(shot.frames_written))
                ? `<span title="Frame files checked on the drive after each piece">${num(shot.frames_written)} of ${num(shot.frames_expected)} frames written</span>`
                : '',
        ].filter(Boolean).join(' · ');
        const actions = [
            open ? `<button class="btn btn-secondary btn-small" data-shot="${esc(shot.shot_id)}" data-action="cancel-shot">Cancel</button>` : '',
            (num(shot.failed) + num(shot.cancelled)) ? `<button class="btn btn-secondary btn-small" data-shot="${esc(shot.shot_id)}" data-action="retry-shot" title="Re-render the failed pieces with the same settings">Retry failed</button>` : '',
            (num(shot.failed) + num(shot.cancelled)) ? `<button class="btn btn-secondary btn-small" data-shot="${esc(shot.shot_id)}" data-action="edit-shot" title="Change settings or computers, then retry the failed pieces"><i class="fas fa-pen"></i> Edit</button>` : '',
        ].join('');
        return `
            <tr>
                <td class="mono" title="${esc(shot.sequence)}">${esc(shotName(shot.sequence))}</td>
                <td class="mono nowrap">${num(shot.frame_start)}–${num(shot.frame_end)}</td>
                <td><div class="shot-progress">
                    <div class="progress-bar-wrapper"><div class="progress-bar" data-width="${num(shot.percent)}"></div></div>
                    <span class="muted-small">${num(shot.percent)}%</span>
                </div></td>
                <td>${parts}</td>
                <td class="nowrap">${esc(shot.updated_at)}</td>
                <td><div class="shot-actions">${actions}</div></td>
            </tr>`;
    }).join('');
    applyWidths(grid);
}

async function loadQueue() {
    try {
        const data = await getJson('/get-queue');
        const c = data.counts || {};

        const waiting = num(c.QUEUED) + num(c.ASSIGNED);
        const badge = $("queue-badge");
        badge.textContent = waiting ? String(waiting) : '';
        badge.classList.toggle('has-items', waiting > 0);
        $("queue-summary").textContent =
            `${num(c.QUEUED)} waiting · ${num(c.ASSIGNED)} rendering · ${num(c.FAILED)} failed`;

        if (activeTab !== 'queue') return;
        renderShots(data.shots || []);
        const jobs = data.jobs || [];
        $("queue-empty").classList.toggle('hidden', jobs.length > 0);
        const table = $("queue-table");
        table.innerHTML = jobs.map(job => `
            <tr>
                <td>${num(job.id)}</td>
                <td class="mono" title="${esc(job.sequence)}">${job.kind && job.kind !== 'render' ? '<i class="fas fa-fire" title="Preparing the cache"></i> ' : ''}${esc(job.kind === 'prepare-fill' ? 'Whole project' : shotName(job.sequence))}
                    ${job.detail ? `<div class="detail-line">${esc(job.detail)}</div>` : ''}</td>
                <td class="mono nowrap">${frameText(job)}</td>
                <td>${esc(PRIORITIES[job.priority] || job.priority)}</td>
                <td class="nowrap">${statusCell(job)}</td>
                <td class="nowrap">${esc(job.node_name || '—')}</td>
                <td>${num(job.attempts)} / ${num(job.max_attempts)}</td>
                <td class="nowrap">${esc(job.updated_at)}</td>
                <td>${queueActions(job)}</td>
            </tr>
        `).join('');
        applyWidths(table);
    } catch (e) {
        console.error('Queue update failed:', e);
    }
}

async function onShotsClick(e) {
    const btn = e.target.closest('button[data-shot]');
    if (!btn) return;
    if (btn.dataset.action === 'edit-shot') {
        openRetry({shot: btn.dataset.shot});
        return;
    }
    if (btn.dataset.action === 'retry-shot') {
        const {ok, data} = await postJson('/retry-shot', {shot_id: btn.dataset.shot});
        if (!ok) toast(data.error || 'Could not retry', 'error');
        else toast(`Re-rendering ${data.jobs} failed piece(s) with the same settings`);
        loadQueue();
        return;
    }
    if (!await askConfirm('Cancel every piece of this shot that has not finished?', 'Cancel shot')) return;
    const {ok, data} = await postJson('/cancel-shot', {shot_id: btn.dataset.shot});
    if (!ok) toast(data.error || 'That did not work', 'error');
    else toast(`Cancelled ${data.jobs} piece(s)`);
    loadQueue();
}

async function onQueueClick(e) {
    const btn = e.target.closest('button[data-job]');
    if (!btn) return;
    const id = num(btn.dataset.job);
    if (btn.dataset.action === 'edit') {
        openRetry({job: id});
        return;
    }
    if (btn.dataset.action === 'retry') {
        const {ok, data} = await postJson('/retry-job', {id});
        if (!ok) toast(data.error || 'Could not retry', 'error');
        else toast(`Job #${id} is back in the queue with the same settings`);
        loadQueue();
        return;
    }
    if (!await askConfirm(`Cancel job #${id}? If it is rendering, it stops now.`, 'Cancel job')) return;
    const {ok, data} = await postJson('/cancel-job', {id});
    if (!ok) toast(data.error || 'That did not work', 'error');
    else toast(`Job #${id} cancelled`);
    loadQueue();
}

// ------------------------------ Retry window: shows the current settings so they can be fixed
let retryTarget = null;
let retryNodes = new Set();

function renderRetryNodes() {
    const names = Object.keys(registry).sort();
    $("retry-nodes").innerHTML = names.map(n => {
        const state = nodeState(n), on = retryNodes.has(n);
        return `
            <button class="node-pill ${on ? 'active' : 'off'}" data-node="${esc(n)}" type="button">
                <span class="dot dot-${state}"></span>
                <span>${esc(n)}</span>
                <small class="muted">${STATE_WORDS[state]}</small>
                <i class="fas ${on ? 'fa-check-square' : 'fa-square'} pick"></i>
            </button>`;
    }).join('');
}

async function openRetry(target) {
    const query = target.shot ? `shot_id=${encodeURIComponent(target.shot)}` : `id=${num(target.job)}`;
    let job;
    try {
        const res = await fetch(`/get-job?${query}`);
        job = await res.json();
        if (!res.ok) throw new Error(job.error || 'not found');
    } catch (e) {
        toast(`Could not load that render: ${e.message}`, 'error');
        return;
    }
    retryTarget = {...target, job};
    $("retry-title").innerHTML = target.shot
        ? `<i class="fas fa-pen"></i> Edit and retry the failed pieces of ${esc(shotName(job.sequence))}`
        : `<i class="fas fa-pen"></i> Edit and retry job #${num(job.id)}: ${esc(shotName(job.sequence))}`;
    $("retry-node-note").textContent = job.split_mode === 'auto'
        ? 'Tick more computers to cut this shot into more pieces, so it finishes faster.'
        : job.shared ? 'Tick more computers so more pieces render at the same time.'
        : 'Tick every computer that may render it. The first free one takes it.';
    $("retry-add-name").value = '';
    $("retry-add-ip").value = '';
    $("retry-reason").textContent = job.detail ? `Why it stopped: ${job.detail}` : '';
    $("retry-reason").classList.toggle('hidden', !job.detail);
    $("retry-project").value = job.project || '';
    $("retry-map").value = job.map || '';
    $("retry-config").value = job.config || '';
    $("retry-output").value = job.output_dir || '';
    $("retry-sequence").value = job.sequence || '';
    const hasRange = job.frame_start !== null && job.frame_start !== undefined;
    $("retry-frames").value = hasRange && !job.is_piece ? `${job.frame_start}-${job.frame_end}` : '';
    $("retry-share").checked = job.split_mode === 'auto' || job.split_mode === 'auto-split';
    $("retry-priority").value = String(num(job.priority));
    $("retry-retries").value = String(num(job.retries));
    const piece = Boolean(target.shot || job.is_piece);
    $("retry-shot-fields").classList.toggle('hidden', piece);
    $("retry-piece-note").classList.toggle('hidden', !piece);
    $("retry-piece-note").textContent = target.shot
        ? 'Changes apply to every failed or cancelled piece of this shot. Each piece keeps its own frames.'
        : `This is piece ${num(job.chunk_index)} of ${num(job.chunk_count)} (frames ${num(job.frame_start)}–${num(job.frame_end)}) of a shared shot; its frames stay the same.`;
    retryNodes = new Set(job.allowed_nodes && job.allowed_nodes.length ? job.allowed_nodes : Object.keys(registry));
    renderRetryNodes();
    $("retry-modal").classList.remove('hidden');
    $("retry-config").focus();
}

function closeRetry() {
    $("retry-modal").classList.add('hidden');
    retryTarget = null;
}

async function submitRetry() {
    if (!retryTarget) return;
    const changes = {
        project: $("retry-project").value.trim(),
        map: $("retry-map").value.trim(),
        config: $("retry-config").value.trim(),
        output_dir: $("retry-output").value.trim(),
        priority: num($("retry-priority").value),
        retries: num($("retry-retries").value),
        nodes: [...retryNodes],
    };
    const piece = Boolean(retryTarget.shot || retryTarget.job.is_piece);
    if (!piece) {
        changes.sequence = $("retry-sequence").value.trim();
        changes.frames = $("retry-frames").value.trim();
        changes.share = $("retry-share").checked;
    }
    if (!changes.project || !changes.map || !changes.config || (!piece && !changes.sequence)) {
        toast('Project, map, preset and shot cannot be empty', 'warn');
        return;
    }
    if (changes.nodes.length === 0) {
        toast('Pick at least one computer', 'warn');
        return;
    }
    const body = retryTarget.shot ? {shot_id: retryTarget.shot, changes} : {id: num(retryTarget.job.id), changes};
    const btn = $("retry-go");
    btn.disabled = true;
    const {ok, data} = await postJson(retryTarget.shot ? '/retry-shot' : '/retry-job', body);
    btn.disabled = false;
    if (!ok) {
        toast(data.error || 'Could not retry', 'error');
        return;
    }
    toast(retryTarget.shot ? `Re-queued ${data.jobs} piece(s)`
        : data.changed && data.changed.length ? `Retrying with your changes (${data.changed.join(', ').replace(/_/g, ' ')})`
        : 'Retrying with the same settings');
    closeRetry();
    loadQueue();
}

function selectAllRetryNodes() {
    const names = Object.keys(registry);
    const all = names.every(n => retryNodes.has(n));
    retryNodes = all ? new Set() : new Set(names);
    renderRetryNodes();
}

async function addRetryNode() {
    const name = $("retry-add-name").value.trim();
    const ip = $("retry-add-ip").value.trim();
    if (!name || !ip) {
        toast('Type the new computer\'s name and IP address', 'warn');
        return;
    }
    const {ok, data} = await postJson('/add-node', {name, ip});
    if (!ok) {
        toast(data.error || 'Could not add the computer', 'error');
        return;
    }
    registry = await getJson('/get-nodes');
    retryNodes.add(name);
    renderRetryNodes();
    renderPills();
    $("retry-add-name").value = '';
    $("retry-add-ip").value = '';
    toast(`${name} added and ticked. It shows as free once its agent answers.`);
}

function onRetryNodesClick(e) {
    const pill = e.target.closest('.node-pill');
    if (!pill) return;
    const name = pill.dataset.node;
    if (retryNodes.has(name)) retryNodes.delete(name); else retryNodes.add(name);
    renderRetryNodes();
}

// ------------------------------ History
function historyTag(status) {
    if (status === 'SUCCESS') return 'tag-success';
    if (status === 'DISPATCHED') return 'tag-dispatched';
    if (status === 'CANCELLED') return 'tag-muted';
    return 'tag-fail';
}

async function loadHistory() {
    const params = new URLSearchParams({q: $('history-search').value.trim()});
    if ($('history-dispatched').checked) params.set('dispatched', '1');
    const logs = await getJson(`/get-history?${params}`);
    $("history-empty").classList.toggle('hidden', logs.length > 0);
    $("history-table").innerHTML = logs.map(log => `
        <tr>
            <td class="nowrap">${esc(log.timestamp)}</td>
            <td class="nowrap">${esc(log.node_name)}</td>
            <td class="mono" title="${esc(log.sequence)}">${esc(String(log.sequence || '').split('/').pop())}</td>
            <td class="nowrap">
                <span class="status-tag ${historyTag(log.status)}">${esc(log.status === 'DISPATCHED' ? 'SENT' : log.status)}</span>
                ${log.detail && log.status !== 'DISPATCHED' ? `<div class="detail-line">${esc(log.detail)}</div>` : ''}
            </td>
            <td class="nowrap">${esc(log.duration)}</td>
            <td>${num(log.frames_rendered)}</td>
            <td class="mono muted" title="${esc(log.project)}">${esc(fileName(log.project))}</td>
        </tr>
    `).join('');
}

let historyTimer = null;
function onHistorySearch() {
    clearTimeout(historyTimer);
    historyTimer = setTimeout(loadHistory, 250);
}

// ------------------------------ Wiring
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab-btn').forEach(btn =>
        btn.addEventListener('click', () => switchTab(btn.dataset.tab, btn)));
    $("add-sequence").addEventListener('click', () => addSequence().querySelector('.sequence-input').focus());
    $("sequence-container").addEventListener('click', onSequenceClick);
    $("node-list").addEventListener('click', onNodeListClick);
    $("admin-nodes").addEventListener('click', onAdminNodesClick);
    $("add-node").addEventListener('click', addNode);
    $("launch").addEventListener('click', launch);
    $("prepare").addEventListener('click', prepareProject);
    $("save-ddc").addEventListener('click', saveSharedCache);
    $("check-ddc").addEventListener('click', checkSharedCache);
    $("save-output").addEventListener('click', saveOutputRoot);
    $("node-status-grid").addEventListener('click', onStatusGridClick);
    $("queue-table").addEventListener('click', onQueueClick);
    $("shots-table").addEventListener('click', onShotsClick);
    $("retry-nodes").addEventListener('click', onRetryNodesClick);
    $("retry-cancel").addEventListener('click', closeRetry);
    $("retry-go").addEventListener('click', submitRetry);
    $("retry-select-all").addEventListener('click', selectAllRetryNodes);
    $("retry-add-node").addEventListener('click', addRetryNode);
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && retryTarget) closeRetry(); });
    $("refresh-history").addEventListener('click', loadHistory);
    $("reload-now").addEventListener('click', () => location.reload());
    $("history-search").addEventListener('input', onHistorySearch);
    $("history-dispatched").addEventListener('change', loadHistory);
    PREF_FIELDS.concat(PREF_CHECKS).forEach(id => $(id).addEventListener('change', () => savePrefs()));

    restorePrefs();
    addSequence();
    loadNodes().then(updateStatus);
    loadQueue();
    loadSettings();

    // Auto-refresh every 2 seconds
    setInterval(() => { updateStatus(); loadQueue(); }, 2000);
});
