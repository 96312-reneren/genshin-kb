// 运行时冒烟测试: 用最小 DOM 桩执行页面脚本, 捕获异常并检查各标签页渲染结果
const fs = require('fs');

const store = {};
function makeEl(sel) {
  const el = {
    _sel: sel, _html: '', textContent: '', style: {}, dataset: {},
    classList: { _s: new Set(), add(c){this._s.add(c)}, remove(c){this._s.delete(c)},
                 toggle(c,v){ if(v===undefined){ this._s.has(c)?this._s.delete(c):this._s.add(c);} else { v?this._s.add(c):this._s.delete(c);} },
                 contains(c){return this._s.has(c)} },
    appendChild(c){ (this._kids=this._kids||[]).push(c); return c; },
    querySelectorAll(){ return []; },
    querySelector(){ return makeEl('sub'); },
    addEventListener(){}, removeEventListener(){},
    set innerHTML(v){ this._html = v; }, get innerHTML(){ return this._html; },
    set onclick(f){ this._onclick = f; }, get onclick(){ return this._onclick; },
    set oninput(f){ this._oninput = f; }, get oninput(){ return this._oninput; },
    set onchange(f){ this._onchange = f; }, get onchange(){ return this._onchange; },
    closest(){ return null; },
    get value(){ return this._v||''; }, set value(v){ this._v = v; },
  };
  return el;
}
function q(sel){ if(!store[sel]) store[sel] = makeEl(sel); return store[sel]; }

const document = {
  querySelector: q,
  querySelectorAll: () => [],
  getElementById: (id) => q('#'+id),
  createElement: (t) => makeEl(t),
  addEventListener: () => {},
};
const window = { scrollTo: () => {} };

// 优先读取抽出的脚本副本; 若不存在, 直接从构建产物 index.html 中提取最后一个 <script> 块。
let src;
if (fs.existsSync('site/_check.js')) {
  src = fs.readFileSync('site/_check.js', 'utf8');
  console.log('脚本来源: site/_check.js');
} else {
  const html = fs.readFileSync('site/index.html', 'utf8');
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (!blocks.length) { console.log('❌ 未能从 site/index.html 中提取脚本'); process.exit(1); }
  src = blocks[blocks.length - 1][1];
  console.log('脚本来源: site/index.html（内联脚本, 已自动提取）');
}
const t0 = Date.now();
try {
  new Function('document','window','KB_UNUSED', src)(document, window, null);
  console.log('✅ 脚本执行无异常  (' + (Date.now()-t0) + 'ms)');
} catch (e) {
  console.log('❌ 运行时错误: ' + e.message);
  console.log(e.stack.split('\n').slice(0, 6).join('\n'));
  process.exit(1);
}

const tabs = ['overview','teams','abyss','ops','incident','materials','resources','chars','data'];
let ok = true;
for (const t of tabs) {
  const h = store['#tab-'+t] ? store['#tab-'+t].innerHTML : '';
  const len = h.length;
  const bad = /undefined|NaN|\[object Object\]/.test(h);
  if (len < 500 || bad) ok = false;
  console.log((len>=500 && !bad ? '  ✅ ' : '  ❌ ') +
    ('tab-' + t).padEnd(14) + String(len).padStart(7) + ' 字符' +
    (bad ? '   << 含 undefined/NaN/[object Object]' : ''));
}
// 关键内容抽检
const all = tabs.map(t => store['#tab-'+t].innerHTML).join('');
const must = ['深渊','配队','运营','树脂','SQLite','合规','元素反应','神铸定轨','空月祝福','渊月祝福',
  '那维莱特','回退','数值膨胀','社区测算','长期影响','1600',
  '观察材料','人工投喂','第三方接口镜像','日期未知','正文未收录'];
const miss = must.filter(m => all.indexOf(m) < 0);
console.log(miss.length ? '  ❌ 缺少关键内容: ' + miss.join(', ') : '  ✅ 关键内容抽检通过');
console.log(ok && !miss.length ? '\n全部检查通过' : '\n存在问题');
process.exit(ok && !miss.length ? 0 : 1);
