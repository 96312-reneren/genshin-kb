'use strict';
const S = { files: [], preview: [], job: null, timer: null, state: {} };
const $ = (id) => document.getElementById(id);

function toast(msg, ms) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), ms || 2600);
}
function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
function toB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}
async function api(path, payload) {
  const opt = payload
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload) }
    : {};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ error: '返回不是 JSON' }));
  if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

/* ── 界面初始化 ── */
async function loadState() {
  try {
    const st = await api('/api/state');
    S.state = st;
    if (!document.querySelector('#kind option')) {
      st.kind_options.forEach((o) => $('kind').add(new Option(o.label, o.value)));
      st.mode_options.forEach((o) => $('mode').add(new Option(o.label, o.value)));
      $('kind').value = st.defaults.kind;
      $('mode').value = st.defaults.mode;
      if (!$('subject').value) $('subject').value = st.defaults.subject;
    }
    const local = st.local || {};
    const pill = $('localStatus');
    pill.className = 'pill ' + (local.reachable ? 'ok' : 'bad');
    pill.textContent = local.reachable
      ? '本地模型已就绪' + (local.models && local.models.length ? '（' + local.models[0] + '）' : '')
      : '本地模型离线';
    pill.title = local.reachable
      ? '点一下重新检查：' + (local.models || []).join(', ')
      : '点一下重新检查。本地模型要你自己在 WSL 里启动（wsl -d Ubuntu-22.04 -- ollama serve），应用不会替你启动 WSL';
    pill.style.cursor = 'pointer';
    pill.onclick = () => loadState();
    const hint = $('localHint');
    if (local.reachable) {
      hint.textContent = '';
    } else {
      hint.textContent = '本地模型现在连不上。这个应用不会替你启动 WSL —— '
        + '先在 WSL 里手动起它（wsl -d Ubuntu-22.04 -- ollama serve），'
        + '回来点一下右上角的「本地模型离线」重新检查，再点「开始生成」；'
        + '只想验证流程可以勾「试跑」（占位文案，不调模型）。';
      hint.style.color = '#b3562f';
    }
    const tp = $('toolStatus');
    tp.textContent = (st.node ? '转换器 ✓' : '转换器 ✗') + ' · ' + (st.libreoffice ? 'LibreOffice ✓' : 'LibreOffice ✗');
    tp.title = '缺少任一项就只出 PPTX / DOCX，不出 PDF';
    $('outDirHint').textContent = '输出目录：' + st.out_dir + '　|　数据：' + st.db;
    renderArtifacts(st.artifacts || []);
  } catch (err) { toast('读取状态失败：' + err.message); }
}

function renderArtifacts(items) {
  const ul = $('artifacts');
  ul.innerHTML = '';
  if (!items.length) { ul.innerHTML = '<li class="empty">还没有产物</li>'; return; }
  items.forEach((a) => {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = a.label + '　' + a.name;
    const size = document.createElement('span');
    size.className = 'size';
    size.textContent = fmtSize(a.size);
    const open = document.createElement('button');
    open.className = 'open';
    open.textContent = '打开';
    open.onclick = () => reveal(a.path, false);
    const where = document.createElement('button');
    where.className = 'open';
    where.textContent = '定位';
    where.onclick = () => reveal(a.path, true);
    li.append(label, size, open, where);
    ul.append(li);
  });
}
async function reveal(path, select) {
  try { await api('/api/reveal', { path, select: !!select }); }
  catch (err) { toast(err.message); }
}

/* ── 投喂 ── */
function addFiles(list) {
  const files = Array.from(list);
  if (!files.length) return;
  files.forEach((f) => {
    if (f.size > 6 * 1024 * 1024) { toast(f.name + ' 太大了（>6MB），跳过'); return; }
    const reader = new FileReader();
    reader.onload = () => {
      S.files.push({ name: f.name, data_b64: toB64(reader.result) });
      renderFiles();
      toast('已加入 ' + f.name + '，共 ' + S.files.length + ' 份');
    };
    reader.onerror = () => toast('读不了 ' + f.name);
    reader.readAsArrayBuffer(f);
  });
}
function renderFiles() {
  const ul = $('fileList');
  ul.innerHTML = '';
  S.files.forEach((f, i) => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = f.name;
    const x = document.createElement('button');
    x.className = 'x';
    x.textContent = '✕';
    x.title = '移掉这份';
    x.onclick = () => { S.files.splice(i, 1); renderFiles(); };
    li.append(name, x);
    ul.append(li);
  });
}

/* ── 预览 ── */
function parseRequest() {
  return { files: S.files, text: $('pasted').value,
           mode: $('mode').value, kind: $('kind').value,
           subject: $('subject').value };
}
async function doParse() {
  $('parseBtn').disabled = true;
  try {
    const data = await api('/api/parse', parseRequest());
    S.preview = data.items;
    renderPreview(data);
    toast('解析出 ' + data.count + ' 条（' + data.mode_label + '）');
  } catch (err) {
    toast(err.message, 4200);
  } finally { $('parseBtn').disabled = false; }
}
function renderPreview(data) {
  const tbody = document.querySelector('#preview tbody');
  tbody.innerHTML = '';
  const meta = $('previewMeta');
  if (!data.count) { tbody.innerHTML = '<tr class="empty"><td colspan="5">—</td></tr>'; }
  data.items.forEach((it) => {
    const tr = document.createElement('tr');
    const c1 = document.createElement('td'); c1.textContent = it.ordinal;
    const c2 = document.createElement('td');
    const inp = document.createElement('input');
    inp.value = it.date || '';
    inp.placeholder = '未知';
    inp.dataset.ordinal = it.ordinal;
    inp.title = '改成 YYYY-MM-DD；留空 = 日期未知';
    c2.append(inp);
    const c3 = document.createElement('td'); c3.className = 'title'; c3.textContent = it.title;
    const c4 = document.createElement('td');
    const pick = document.createElement('select');
    pick.className = 'kinds';
    pick.dataset.ordinal = it.ordinal;
    (S.state.kind_options || [{ value: it.kind, label: it.kind_label }])
      .forEach((o) => pick.add(new Option(o.label, o.value)));
    pick.value = it.kind;
    pick.title = '这条属于哪一类；只影响时间轴泳道与分类统计';
    c4.append(pick);
    const c5 = document.createElement('td');
    const src = document.createElement('span');
    src.className = 'tag' + (it.date ? '' : ' unk');
    src.textContent = it.date ? (it.date_source || '来自文本') : '日期未知';
    c5.append(src);
    tr.append(c1, c2, c3, c4, c5);
    tbody.append(tr);
  });
  meta.textContent = '共 ' + data.count + ' 条，切分方式：' + data.mode_label
    + '；缺日期 ' + data.missing_dates + ' 条。'
    + (data.warnings && data.warnings.length ? '　提示：' + data.warnings.join('；') : '');
}
function collectKindOverrides() {
  const out = {};
  document.querySelectorAll('#preview tbody select.kinds').forEach((sel) => {
    const shown = S.preview.find((p) => String(p.ordinal) === sel.dataset.ordinal);
    if (shown && sel.value !== shown.kind) out[sel.dataset.ordinal] = sel.value;
  });
  return out;
}
function collectOverrides() {
  const out = {};
  document.querySelectorAll('#preview tbody input').forEach((inp) => {
    const shown = S.preview.find((p) => String(p.ordinal) === inp.dataset.ordinal);
    const before = shown ? (shown.date || '') : '';
    if (inp.value.trim() !== before) out[inp.dataset.ordinal] = inp.value.trim();
  });
  return out;
}

/* ── 生成 ── */
async function doGenerate() {
  const payload = parseRequest();
  payload.overrides = collectOverrides();
  payload.kinds = collectKindOverrides();
  payload.office = $('office').checked;
  payload.deck_plain = $('deckPlain').checked;
  payload.fill = $('fill').checked;
  payload.dry_run = $('dry').checked;
  payload.fresh = $('fresh').checked;
  payload.refresh = $('refresh').checked;
  if ($('cloud').checked) payload.condenser = 'cloud';
  if (!S.files.length && !$('pasted').value.trim()) { toast('先拖入或粘贴一些文本'); return; }
  $('genBtn').disabled = true;
  $('log').textContent = '';
  $('bar').style.width = '3%';
  try {
    const { job_id: jobId } = await api('/api/generate', payload);
    S.job = jobId;
    poll();
  } catch (err) {
    $('genBtn').disabled = false;
    toast('启动失败：' + err.message, 4200);
  }
}
function poll() {
  clearTimeout(S.timer);
  S.timer = setTimeout(async () => {
    if (!S.job) return;
    let job;
    try { job = await api('/api/job/' + S.job); }
    catch (err) { toast('取进度失败：' + err.message); return finish(job || {}); }
    $('log').textContent = (job.log || []).join('\n');
    $('log').scrollTop = $('log').scrollHeight;
    $('bar').style.width = job.status === 'done' ? '100%'
      : Math.min(94, 6 + (job.log || []).length * 6) + '%';
    if (job.status === 'running') { poll(); return; }
    finish(job);
  }, 900);
}
async function finish(job) {
  $('genBtn').disabled = false;
  S.job = null;
  if (job.status === 'done') {
    const s = job.summary || {};
    toast('完成：' + (s.items || 0) + ' 条，用时 ' + (s.elapsed || '?') + 's');
    renderArtifacts(job.artifacts || []);
  } else {
    toast('失败了：' + (job.error || '未知错误'), 6000);
    $('bar').style.width = '100%';
    $('bar').style.background = '#b3562f';
  }
  loadState();
}

/* ── 事件绑定 ── */
(function wire() {
  const dz = $('dropzone');
  dz.onclick = () => $('fileInput').click();
  dz.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') $('fileInput').click(); };
  $('fileInput').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };
  ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.add('hot');
  }));
  ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove('hot');
  }));
  dz.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.target.closest && e.target.closest('#dropzone')) return;
    addFiles(e.dataTransfer.files);
  });

  $('parseBtn').onclick = doParse;
  $('clearBtn').onclick = () => {
    S.files = []; S.preview = [];
    $('pasted').value = '';
    renderFiles();
    renderPreview({ count: 0, items: [] });
    toast('已清空');
  };
  $('genBtn').onclick = doGenerate;
  $('revealOut').onclick = () => reveal(S.state.out_dir || '', false);
  $('quitBtn').onclick = async () => {
    if (!confirm('关闭工作台？')) return;
    try { await api('/api/quit', {}); } catch (e) { /* 服务已经在关了 */ }
    document.body.innerHTML = '<p style="padding:40px;font:16px sans-serif">'
      + '工作台已关闭，这个页面可以关掉了。</p>';
  };
  loadState();
})();
