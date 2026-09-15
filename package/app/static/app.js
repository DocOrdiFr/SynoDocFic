const state = {
  mainTab: 'renommer',
  subTab: 'remove',
  previewId: null,
  previewItems: [],
  browsePath: '/',
};

const els = {
  mainTabs: document.querySelectorAll('.main-tab'),
  mainPanels: document.querySelectorAll('.main-panel'),
  subTabs: document.querySelectorAll('.sub-tab'),
  subPanels: document.querySelectorAll('.sub-panel'),
  directoryInput: document.getElementById('directoryInput'),
  recursiveInput: document.getElementById('recursiveInput'),
  removeValue: document.getElementById('removeValue'),
  prefixValue: document.getElementById('prefixValue'),
  suffixValue: document.getElementById('suffixValue'),
  previewBtn: document.getElementById('previewBtn'),
  applyBtn: document.getElementById('applyBtn'),
  undoBtn: document.getElementById('undoBtn'),
  previewBody: document.getElementById('previewBody'),
  summaryBadge: document.getElementById('summaryBadge'),
  browseBtn: document.getElementById('browseBtn'),
  browserPanel: document.getElementById('browserPanel'),
  browserPath: document.getElementById('browserPath'),
  browserUpBtn: document.getElementById('browserUpBtn'),
  directoryList: document.getElementById('directoryList'),
  historyList: document.getElementById('historyList'),
  refreshHistoryBtn: document.getElementById('refreshHistoryBtn'),
  toast: document.getElementById('toast'),
};

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove('hidden');
  window.setTimeout(() => els.toast.classList.add('hidden'), 2600);
}

function getModeAndValue() {
  if (state.subTab === 'remove') {
    return { mode: 'remove', value: els.removeValue.value.trim() };
  }
  if (state.subTab === 'prefix') {
    return { mode: 'prefix', value: els.prefixValue.value };
  }
  return { mode: 'suffix', value: els.suffixValue.value };
}

function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

async function apiGet(url) {
  const res = await fetch(url, { method: 'GET' });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || 'Requête échouée');
  }
  return data;
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || 'Requête échouée');
  }
  return data;
}

function setMainTab(tabName) {
  state.mainTab = tabName;
  els.mainTabs.forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.main === tabName);
  });
  els.mainPanels.forEach((panel) => {
    panel.classList.toggle('active', panel.dataset.mainPanel === tabName);
  });
}

function setSubTab(tabName) {
  state.subTab = tabName;
  els.subTabs.forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.subtab === tabName);
  });
  els.subPanels.forEach((panel) => {
    panel.classList.toggle('active', panel.dataset.subtabPanel === tabName);
  });
}

function renderPreview(items, summary) {
  state.previewItems = items;

  if (!items.length) {
    els.previewBody.innerHTML = '<tr><td colspan="3" class="muted">Aucun fichier trouvé pour cette sélection.</td></tr>';
  } else {
    els.previewBody.innerHTML = items
      .map((item) => {
        const statusClass = `status-${item.status}`;
        const statusLabel = item.status === 'ready' ? 'Prêt' : item.status === 'unchanged' ? 'Inchangé' : item.status;
        return `
          <tr>
            <td>${escapeHtml(item.from)}</td>
            <td>${escapeHtml(item.to)}</td>
            <td class="${statusClass}">${escapeHtml(statusLabel)}</td>
          </tr>
        `;
      })
      .join('');
  }

  const txt = `${summary.rename_actions} à renommer, ${summary.unchanged} inchangés, ${summary.auto_renamed} auto-résolus` + (summary.truncated ? ' (tronqué)' : '');
  els.summaryBadge.textContent = txt;
}

async function generatePreview() {
  const directory = els.directoryInput.value.trim();
  const recursive = els.recursiveInput.checked;
  const { mode, value } = getModeAndValue();

  if (!directory) {
    showToast('Veuillez sélectionner un répertoire.');
    return;
  }

  const payload = { directory, recursive, mode, value, limit: 20000 };

  els.previewBtn.disabled = true;
  els.applyBtn.disabled = true;
  try {
    const data = await apiPost('/api/preview', payload);
    state.previewId = data.preview_id;
    renderPreview(data.items, data.summary);
    els.applyBtn.disabled = data.summary.rename_actions === 0;
    showToast('Aperçu généré.');
  } catch (error) {
    showToast(error.message);
  } finally {
    els.previewBtn.disabled = false;
  }
}

async function applyRename() {
  if (!state.previewId) {
    showToast('Générez un aperçu avant d\'appliquer.');
    return;
  }

  const confirmed = window.confirm('Appliquer les renommages ? L\'opération est annulable via le bouton Annuler le dernier lot.');
  if (!confirmed) {
    return;
  }

  els.applyBtn.disabled = true;
  try {
    const data = await apiPost('/api/rename', { preview_id: state.previewId, confirm: true });
    showToast(`Renommage terminé : ${data.renamed_count} fichiers.`);
    state.previewId = null;
    await loadHistory();
  } catch (error) {
    showToast(error.message);
  } finally {
    els.applyBtn.disabled = false;
  }
}

async function undoLastBatch() {
  const confirmed = window.confirm('Annuler le dernier lot de renommages ?');
  if (!confirmed) {
    return;
  }

  els.undoBtn.disabled = true;
  try {
    const data = await apiPost('/api/undo', {});
    showToast(`Annulation effectuée : ${data.undo_count} fichiers restaurés.`);
    await loadHistory();
  } catch (error) {
    showToast(error.message);
  } finally {
    els.undoBtn.disabled = false;
  }
}

async function loadDirectories(path = '') {
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    const data = await apiGet(`/api/dirs${query}`);
    state.browsePath = data.path;
    els.browserPath.textContent = data.path;
    els.directoryList.innerHTML = '';

    if (!data.directories.length) {
      els.directoryList.innerHTML = '<div class="muted">Aucun sous-dossier.</div>';
    } else {
      data.directories.forEach((dirPath) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'directory-item';
        btn.textContent = dirPath;
        btn.addEventListener('click', () => {
          els.directoryInput.value = dirPath;
          loadDirectories(dirPath);
        });
        els.directoryList.appendChild(btn);
      });
    }

    els.browserUpBtn.onclick = () => {
      if (!data.parent || data.parent === data.path) {
        loadDirectories('');
        return;
      }
      if (data.parent === '/') {
        loadDirectories('');
      } else {
        loadDirectories(data.parent);
      }
    };
  } catch (error) {
    showToast(error.message);
  }
}

async function loadHistory() {
  try {
    const data = await apiGet('/api/history?limit=20');
    const items = data.items || [];
    if (!items.length) {
      els.historyList.innerHTML = '<div class="muted">Aucune opération effectuée.</div>';
      return;
    }

    els.historyList.innerHTML = items
      .map((item) => {
        const req = item.request || {};
        const op = `${req.mode || 'n/a'}: ${req.value || ''}`.trim();
        const count = (item.entries || []).filter((e) => e.status === 'renamed').length;
        const undone = item.undone ? ' (annulé)' : '';
        return `
          <div class="history-item">
            <div><strong>${escapeHtml(item.batch_id)}</strong>${escapeHtml(undone)}</div>
            <div class="muted">${escapeHtml(req.directory || '')}</div>
            <div class="muted">${escapeHtml(op)} | ${count} renommés</div>
            <div class="muted">${escapeHtml(item.created_at_utc || '')}</div>
          </div>
        `;
      })
      .join('');
  } catch (error) {
    showToast(error.message);
  }
}

function initEvents() {
  initCollapsible();
  initSyncProtocolToggles();
  initSyncBrowsers();
  initCompareModal();
  els.mainTabs.forEach((tab) => {
    tab.addEventListener('click', () => setMainTab(tab.dataset.main));
  });

  els.subTabs.forEach((tab) => {
    tab.addEventListener('click', () => setSubTab(tab.dataset.subtab));
  });

  els.previewBtn.addEventListener('click', generatePreview);
  els.applyBtn.addEventListener('click', applyRename);
  els.undoBtn.addEventListener('click', undoLastBatch);

  els.browseBtn.addEventListener('click', async () => {
    els.browserPanel.classList.toggle('hidden');
    await loadDirectories(els.directoryInput.value.trim());
  });

  els.refreshHistoryBtn.addEventListener('click', loadHistory);

  const syncAddProfileBtn = document.getElementById('syncAddProfileBtn');
  if (syncAddProfileBtn) syncAddProfileBtn.addEventListener('click', addSyncProfile);

  const syncEditCancelBtn = document.getElementById('syncEditCancelBtn');
  if (syncEditCancelBtn) syncEditCancelBtn.addEventListener('click', cancelEditProfile);
}

// Sync-related functions
const syncState = {
  profiles: [],
  running: false,
};

async function loadSyncProfiles() {
  try {
    const data = await apiGet('/api/sync/profiles');
    syncState.profiles = data.profiles || [];
    renderSyncProfiles();
  } catch (error) {
    showToast('Erreur lors du chargement des profils : ' + error.message);
  }
}

function renderSyncProfiles() {
  const list = document.getElementById('syncProfilesList');
  if (!syncState.profiles.length) {
    list.innerHTML = '<p class="muted">Aucun profil de synchronisation créé.</p>';
    return;
  }

  list.innerHTML = syncState.profiles
    .map((profile) => {
      const src = profile.source_path || '';
      const dst = profile.dest_path || '';
      const srcProto = profile.source_protocol || profile.protocol || 'local';
      const dstProto = profile.dest_protocol || profile.protocol || 'local';
      const srcHost = profile.source_host ? `${profile.source_host}:` : '';
      const dstHost = profile.dest_host ? `${profile.dest_host}:` : '';
      const protoLabel = (p) => p === 'local' ? '📂' : '🌐 ' + p.toUpperCase();
      return `
        <div class="profile-item">
          <div class="profile-info">
            <div class="profile-name">${escapeHtml(profile.name)}</div>
            <div class="profile-path">${protoLabel(srcProto)} ${escapeHtml(srcHost + src)} → ${protoLabel(dstProto)} ${escapeHtml(dstHost + dst)}</div>
          </div>
          <div class="profile-actions">
            <button class="btn-small btn-run" onclick="compareProfile('${escapeHtml(profile.id)}')">&#x1F50D;&#x25B6; Comparer et lancer</button>
            <button class="btn-small btn-edit" onclick="editProfile('${escapeHtml(profile.id)}')">&#x270F;&#xFE0F; Modifier</button>
            <button class="btn-small btn-delete" onclick="deleteSyncProfile('${escapeHtml(profile.id)}')">✕ Supprimer</button>
          </div>
        </div>
      `;
    })
    .join('');
}

function initSyncProtocolToggles() {
  ['Source', 'Dest'].forEach((ep) => {
    const sel = document.getElementById(`sync${ep}Protocol`);
    if (!sel) return;
    sel.addEventListener('change', () => {
      const isRemote = sel.value !== 'local';
      document.getElementById(`sync${ep}Remote`).style.display = isRemote ? '' : 'none';
      document.getElementById(`sync${ep}HostWrap`).style.display = isRemote ? '' : 'none';
      document.getElementById(`sync${ep}Browser`).classList.add('hidden');
    });
  });
}

const syncBrowserData = { Source: null, Dest: null };

async function loadSyncBrowser(ep, path) {
  const proto = document.getElementById(`sync${ep}Protocol`).value;
  const browserEl = document.getElementById(`sync${ep}Browser`);
  const pathEl = document.getElementById(`sync${ep}BrowserPath`);
  const listEl = document.getElementById(`sync${ep}DirList`);

  browserEl.classList.remove('hidden');
  listEl.innerHTML = '<div class="muted">Chargement…</div>';

  try {
    let data;
    if (proto === 'local') {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      data = await apiGet(`/api/dirs${query}`);
    } else {
      const host = document.getElementById(`sync${ep}Host`)?.value?.trim();
      if (!host) {
        showToast('Renseignez d\'abord le champ Hôte / IP.');
        browserEl.classList.add('hidden');
        return;
      }
      const user = document.getElementById(`sync${ep}User`)?.value?.trim();
      const pass = document.getElementById(`sync${ep}Pass`)?.value?.trim();
      data = await apiPost('/api/sync/browse', {
        protocol: proto,
        host,
        path: path || '/',
        user: user || '',
        password: pass || '',
      });
    }

    syncBrowserData[ep] = data;
    pathEl.textContent = data.path;
    listEl.innerHTML = '';

    if (!data.directories || !data.directories.length) {
      listEl.innerHTML = '<div class="muted">Aucun sous-dossier.</div>';
    } else {
      data.directories.forEach((dirPath) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'directory-item';
        btn.textContent = dirPath;
        btn.addEventListener('click', () => {
          const inputId = ep === 'Source' ? 'syncSource' : 'syncDest';
          const inp = document.getElementById(inputId);
          if (inp) inp.value = dirPath;
          loadSyncBrowser(ep, dirPath);
        });
        listEl.appendChild(btn);
      });
    }

    document.getElementById(`sync${ep}BrowserUp`).onclick = () => {
      if (data.parent && data.parent !== data.path) {
        loadSyncBrowser(ep, data.parent === '/' ? '' : data.parent);
      }
    };
  } catch (error) {
    listEl.innerHTML = `<div class="muted" style="color:#c0392b;">${escapeHtml(error.message)}</div>`;
  }
}

function initSyncBrowsers() {
  ['Source', 'Dest'].forEach((ep) => {
    const btn = document.getElementById(`sync${ep}BrowseBtn`);
    if (!btn) return;
    btn.addEventListener('click', () => {
      const browserEl = document.getElementById(`sync${ep}Browser`);
      if (!browserEl.classList.contains('hidden')) {
        browserEl.classList.add('hidden');
        return;
      }
      const inputId = ep === 'Source' ? 'syncSource' : 'syncDest';
      const currentPath = document.getElementById(inputId)?.value?.trim() || '';
      loadSyncBrowser(ep, currentPath);
    });
  });
}

async function addSyncProfile() {
  const btn = document.getElementById('syncAddProfileBtn');
  const editId = btn?.dataset?.editId || null;

  const name = (document.getElementById('syncName') || {}).value?.trim();
  const sourceProtocol = (document.getElementById('syncSourceProtocol') || {}).value || 'local';
  const destProtocol = (document.getElementById('syncDestProtocol') || {}).value || 'local';
  const source = (document.getElementById('syncSource') || {}).value?.trim();
  const dest = (document.getElementById('syncDest') || {}).value?.trim();
  const sourceHost = (document.getElementById('syncSourceHost') || {}).value?.trim();
  const destHost = (document.getElementById('syncDestHost') || {}).value?.trim();
  const sourceUser = (document.getElementById('syncSourceUser') || {}).value?.trim();
  const sourcePass = (document.getElementById('syncSourcePass') || {}).value?.trim();
  const destUser = (document.getElementById('syncDestUser') || {}).value?.trim();
  const destPass = (document.getElementById('syncDestPass') || {}).value?.trim();
  const recursive = (document.getElementById('syncRecursive') || {}).checked;

  if (!name || !source || !dest) {
    showToast('Veuillez remplir tous les champs obligatoires.');
    return;
  }

  const payload = {
    name,
    source_protocol: sourceProtocol,
    dest_protocol: destProtocol,
    source_host: sourceHost || '',
    dest_host: destHost || '',
    source_path: source,
    dest_path: dest,
    source_user: sourceUser || '',
    source_pass: sourcePass || '',
    dest_user: destUser || '',
    dest_pass: destPass || '',
    recursive,
  };

  try {
    if (editId) {
      payload.profile_id = editId;
      await apiPost('/api/sync/update-profile', payload);
      showToast('Profil mis à jour.');
      cancelEditProfile();
    } else {
      const data = await apiPost('/api/sync/profiles', payload);
      showToast('Profil ajouté : ' + data.name);
      ['syncName', 'syncSource', 'syncDest', 'syncSourceHost', 'syncDestHost',
       'syncSourceUser', 'syncSourcePass', 'syncDestUser', 'syncDestPass'].forEach((id) => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
      ['syncSourceProtocol', 'syncDestProtocol'].forEach((id) => {
        const el = document.getElementById(id); if (el) el.value = 'local';
      });
      ['syncSourceRemote', 'syncDestRemote', 'syncSourceHostWrap', 'syncDestHostWrap'].forEach((id) => {
        const el = document.getElementById(id); if (el) el.style.display = 'none';
      });
      ['syncSourceBrowser', 'syncDestBrowser'].forEach((id) => {
        const el = document.getElementById(id); if (el) el.classList.add('hidden');
      });
    }
    await loadSyncProfiles();
  } catch (error) {
    showToast('Erreur : ' + error.message);
  }
}

async function runSyncProfile(profileId, selectedRels) {
  if (syncState.running) {
    showToast('Une synchronisation est déjà en cours.');
    return;
  }

  syncState.running = true;
  try {
    const payload = { profile_id: profileId };
    if (Array.isArray(selectedRels)) payload.selected_rels = selectedRels;
    const data = await apiPost('/api/sync/run', payload);
    const panel = document.getElementById('syncStatusPanel');
    const content = document.getElementById('syncStatusContent');
    panel.style.display = 'block';
    content.innerHTML = `
      <p><strong>Synchronisation terminée !</strong></p>
      <p>Fichiers copiés : ${data.files_copied || 0}</p>
      <p>Fichiers ignorés (identiques) : ${data.files_skipped || 0}</p>
      ${data.files_excluded ? `<p>Fichiers non sélectionnés : ${data.files_excluded}</p>` : ''}
      <p>Erreurs : ${data.errors || 0}</p>
      ${data.error_messages ? '<p style="color: #c0392b;">' + escapeHtml(data.error_messages.join('; ')) + '</p>' : ''}
    `;
    showToast('Synchronisation terminée.');
  } catch (error) {
    showToast('Erreur de synchronisation : ' + error.message);
  } finally {
    syncState.running = false;
  }
}

async function deleteSyncProfile(profileId) {
  const confirmed = window.confirm('Supprimer ce profil de synchronisation ?');
  if (!confirmed) return;

  try {
    await apiPost('/api/sync/delete-profile', { profile_id: profileId });
    showToast('Profil supprimé.');
    await loadSyncProfiles();
  } catch (error) {
    showToast('Erreur lors de la suppression : ' + error.message);
  }
}

function init() {
  initEvents();
  loadHistory();
  loadSyncProfiles();
}

init();

/* ===== COLLAPSIBLE CARDS ===== */
function initCollapsible() {
  document.querySelectorAll('.collapse-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      btn.closest('.card').classList.toggle('collapsed');
    });
  });
}

/* ===== COMPARE MODAL ===== */
let compareCurrentProfileId = null;
let compareSelectableFiles = []; // flat list of {rel} for every checkbox shown

function initCompareModal() {
  document.getElementById('compareCloseBtn')?.addEventListener('click', closeCompareModal);
  document.getElementById('compareCloseBtnBottom')?.addEventListener('click', closeCompareModal);
  document.getElementById('compareOverlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeCompareModal();
  });
  document.getElementById('compareModalLaunchBtn')?.addEventListener('click', () => {
    const selectedRels = compareSelectableFiles
      .filter((_, idx) => document.querySelector(`.compare-check[data-index="${idx}"]`)?.checked)
      .map((f) => f.rel);
    if (!selectedRels.length) {
      showToast('Sélectionnez au moins un fichier à copier.');
      return;
    }
    closeCompareModal();
    if (compareCurrentProfileId) runSyncProfile(compareCurrentProfileId, selectedRels);
  });
  document.getElementById('compareSelectAllBtn')?.addEventListener('click', () => setAllCompareChecks(true));
  document.getElementById('compareDeselectAllBtn')?.addEventListener('click', () => setAllCompareChecks(false));
  document.getElementById('compareModalDetails')?.addEventListener('change', (e) => {
    if (e.target.classList.contains('compare-check')) updateCompareSelectedCount();
  });
  document.getElementById('compareDownloadBtn')?.addEventListener('click', () => {
    if (compareCurrentProfileId) downloadListing(compareCurrentProfileId);
  });
}

function setAllCompareChecks(checked) {
  document.querySelectorAll('.compare-check').forEach((el) => { el.checked = checked; });
  updateCompareSelectedCount();
}

function updateCompareSelectedCount() {
  const total = compareSelectableFiles.length;
  const checked = document.querySelectorAll('.compare-check:checked').length;
  const el = document.getElementById('compareSelectedCount');
  if (el) el.textContent = `${checked} / ${total} sélectionné(s)`;
}

function closeCompareModal() {
  document.getElementById('compareOverlay').classList.add('hidden');
}

function formatSize(bytes) {
  if (!bytes) return '0 o';
  if (bytes < 1024) return bytes + ' o';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' Ko';
  if (bytes < 1024 ** 3) return (bytes / 1024 / 1024).toFixed(1) + ' Mo';
  return (bytes / 1024 ** 3).toFixed(2) + ' Go';
}

async function compareProfile(profileId) {
  compareCurrentProfileId = profileId;
  compareSelectableFiles = [];
  const profile = syncState.profiles.find((p) => p.id === profileId);
  document.getElementById('compareModalTitle').textContent = `Comparaison — ${profile?.name || profileId}`;
  document.getElementById('compareModalSubtitle').textContent =
    `${profile?.source_path || ''} → ${profile?.dest_path || ''}`;
  document.getElementById('compareModalSummary').innerHTML = '<div class="muted" style="padding:4px 0;">Analyse en cours…</div>';
  document.getElementById('compareModalDetails').innerHTML = '';
  document.getElementById('compareModalLaunchBtn').style.display = 'none';
  document.getElementById('compareSelectAllBar').style.display = 'none';
  document.getElementById('compareOverlay').classList.remove('hidden');

  try {
    const data = await apiPost('/api/sync/compare', { profile_id: profileId });

    document.getElementById('compareModalSummary').innerHTML = `
      <div class="compare-stat copy"><span class="stat-value">${data.to_copy.length}</span><span class="stat-label">à copier</span></div>
      <div class="compare-stat update"><span class="stat-value">${data.to_update.length}</span><span class="stat-label">à mettre à jour</span></div>
      <div class="compare-stat ok"><span class="stat-value">${data.up_to_date}</span><span class="stat-label">déjà à jour</span></div>
    `;

    const checkboxFor = (f) => {
      const idx = compareSelectableFiles.length;
      compareSelectableFiles.push({ rel: f.rel });
      return `<input type="checkbox" class="compare-check" data-index="${idx}" checked>`;
    };

    let html = '';
    if (data.to_copy.length) {
      html += `<details${!data.to_update.length ? ' open' : ''}>
        <summary>&#x1F4C4; À copier (${data.to_copy.length})</summary>
        <div class="compare-file-list">${data.to_copy.map((f) =>
          `<div class="compare-file-item">${checkboxFor(f)}<span class="compare-file-name">${escapeHtml(f.rel)}</span><span class="compare-file-meta">${formatSize(f.size)}</span></div>`
        ).join('')}</div></details>`;
    }
    if (data.to_update.length) {
      html += `<details open>
        <summary>&#x1F504; À mettre à jour (${data.to_update.length})</summary>
        <div class="compare-file-list">${data.to_update.map((f) =>
          `<div class="compare-file-item">${checkboxFor(f)}<span class="compare-file-name">${escapeHtml(f.rel)}</span><span class="compare-file-meta">${escapeHtml(f.reason)}</span></div>`
        ).join('')}</div></details>`;
    }
    if (!data.to_copy.length && !data.to_update.length) {
      html = '<div class="muted" style="padding:12px 0;">✅ Source et destination sont synchronisées.</div>';
    }
    if (data.errors?.length) {
      html += `<div style="color:#c0392b;padding:8px 0;font-size:.88rem">⚠ ${data.errors.map(escapeHtml).join('<br>')}</div>`;
    }
    document.getElementById('compareModalDetails').innerHTML = html;
    if (compareSelectableFiles.length) {
      document.getElementById('compareModalLaunchBtn').style.display = '';
      document.getElementById('compareSelectAllBar').style.display = '';
      updateCompareSelectedCount();
    }
    document.getElementById('compareDownloadBtn').style.display = '';
  } catch (error) {
    document.getElementById('compareModalSummary').innerHTML =
      `<div class="muted" style="color:#c0392b;">Erreur : ${escapeHtml(error.message)}</div>`;
  }
}

async function downloadListing(profileId) {
  try {
    const res = await fetch('/api/sync/dump-listing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: profileId }),
    });
    if (!res.ok) throw new Error(await res.text());
    const text = await res.text();
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `listing_${profileId.slice(0, 8)}_${new Date().toISOString().slice(0, 10)}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (error) {
    showToast('Erreur téléchargement : ' + error.message);
  }
}

/* ===== EDIT PROFILE ===== */
let editingProfileId = null;

function editProfile(profileId) {
  const profile = syncState.profiles.find((p) => p.id === profileId);
  if (!profile) return;
  editingProfileId = profileId;

  document.getElementById('syncName').value = profile.name || '';
  document.getElementById('syncSourceProtocol').value = profile.source_protocol || 'local';
  document.getElementById('syncDestProtocol').value = profile.dest_protocol || 'local';
  document.getElementById('syncSourceHost').value = profile.source_host || '';
  document.getElementById('syncDestHost').value = profile.dest_host || '';
  document.getElementById('syncSource').value = profile.source_path || '';
  document.getElementById('syncDest').value = profile.dest_path || '';
  document.getElementById('syncSourceUser').value = profile.source_user || '';
  document.getElementById('syncSourcePass').value = '';
  document.getElementById('syncDestUser').value = profile.dest_user || '';
  document.getElementById('syncDestPass').value = '';
  document.getElementById('syncRecursive').checked = !!profile.recursive;

  ['Source', 'Dest'].forEach((ep) => {
    const proto = document.getElementById(`sync${ep}Protocol`).value;
    const isRemote = proto !== 'local';
    document.getElementById(`sync${ep}Remote`).style.display = isRemote ? '' : 'none';
    document.getElementById(`sync${ep}HostWrap`).style.display = isRemote ? '' : 'none';
  });

  const btn = document.getElementById('syncAddProfileBtn');
  btn.textContent = '💾 Mettre à jour le profil';
  btn.dataset.editId = profileId;
  document.getElementById('syncEditCancelBtn').style.display = '';

  const formCard = btn.closest('.card');
  formCard.classList.remove('collapsed');
  formCard.scrollIntoView({ behavior: 'smooth', block: 'start' });

  document.querySelectorAll('.profile-item').forEach((el) => el.classList.remove('editing'));
  document.querySelectorAll(`[onclick*="editProfile('${profileId}')"]`)
    .forEach((el) => el.closest('.profile-item')?.classList.add('editing'));
}

function cancelEditProfile() {
  editingProfileId = null;
  const btn = document.getElementById('syncAddProfileBtn');
  btn.textContent = '➕ Ajouter ce profil';
  delete btn.dataset.editId;
  document.getElementById('syncEditCancelBtn').style.display = 'none';
  document.querySelectorAll('.profile-item.editing').forEach((el) => el.classList.remove('editing'));
  ['syncName', 'syncSource', 'syncDest', 'syncSourceHost', 'syncDestHost',
   'syncSourceUser', 'syncSourcePass', 'syncDestUser', 'syncDestPass'].forEach((id) => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  ['syncSourceProtocol', 'syncDestProtocol'].forEach((id) => {
    const el = document.getElementById(id); if (el) el.value = 'local';
  });
  ['syncSourceRemote', 'syncDestRemote', 'syncSourceHostWrap', 'syncDestHostWrap'].forEach((id) => {
    const el = document.getElementById(id); if (el) el.style.display = 'none';
  });
}
